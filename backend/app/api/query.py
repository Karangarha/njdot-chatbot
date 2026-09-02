"""POST /api/query — the single RAG endpoint for the NJDOT Chatbot.

Pipeline (per request)
----------------------
1. HybridRanker   – retrieves the top-5 chunks via RRF (vector + keyword)
2. PromptBuilder  – assembles the system prompt and user message
3. LLMClient      – calls gpt-4o-mini with temperature=0
4. CitationSerializer – parses JSON response and validates citations

All pipeline objects are module-level singletons initialised once when
the module is first imported (i.e. when uvicorn loads the app).  They
share a single Supabase client and a single OpenAI client to avoid
redundant connection handshakes.

Error policy
------------
* HTTP 400 – ``query`` field is missing, empty, or blank
  (enforced by the Pydantic ``QueryRequest`` validator before reaching here)
* HTTP 500 – any unexpected exception raised inside the pipeline;
  the ``detail`` string includes the exception type and message so the
  caller can log it without inspecting server logs.
"""

from __future__ import annotations

import asyncio
import re
import time
import logging
from functools import partial
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, HTTPException

from app.database                        import get_db
from app.retrieval.vector_search         import VectorSearcher
from app.retrieval.bm25_search           import KeywordSearcher
from app.retrieval.hybrid_ranker         import HybridRanker, classify_query
from app.retrieval.query_expander        import QueryExpander
from app.retrieval.bdc_matcher           import get_bdc_matcher
from app.generation.llm_client           import LLMClient
from app.generation.prompt_builder       import PromptBuilder
from app.generation.citation_serializer  import CitationSerializer
from app.models                          import (
    BDCAlertItem, CitationItem, QueryRequest, QueryResponse,
    DebugChunkItem, DebugResponse,
)

# ── Cross-reference detection ─────────────────────────────────────────────────
#
# NJDOT specs constantly point to other sections: "as specified in 902.02" or
# "see 401.03.07". When the retrieved chunk contains such a pointer, the LLM
# sees the reference but not the content it points to — so it can't answer.
#
# This regex finds section numbers in retrieved chunk text. We then do a
# second retrieval pass to fetch those target sections.
#
_XREF_RE = re.compile(r'\b(\d{3}\.\d{2}(?:\.\d{2})?)\b')

logger = logging.getLogger(__name__)

# ── Pipeline singletons (initialised once at import time) ─────────────────────
#
# Sharing db_client and oai_client across components avoids opening more
# than one connection to Supabase and one to OpenAI per worker process.
#
_db          = get_db()
_vector      = VectorSearcher(db_client=_db)
_keyword     = KeywordSearcher(db_client=_db)
_hybrid      = HybridRanker(vector_searcher=_vector, keyword_searcher=_keyword)
_bdc_matcher = get_bdc_matcher()
_builder     = PromptBuilder()
_llm         = LLMClient()
_serializer  = CitationSerializer()
_expander    = QueryExpander(llm_client=_llm, hybrid_ranker=_hybrid)

# ── Router ────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api")

_RETRIEVE_K: int = 12   # chunks passed to the LLM context (covers main subpart + Division 900 cross-refs)
_DEBUG_K:    int = 25   # larger candidate pool for debug — reveals all ranked candidates
_MAX_XREFS:  int = 3    # maximum cross-referenced sections to follow per query


def _fetch_cross_ref_chunks(
    chunks:     List[Dict[str, Any]],
    collection: Optional[str],
    already:    Set[str],
) -> List[Dict[str, Any]]:
    """
    Scan retrieved chunks for cross-reference pointers and fetch their targets.

    HOW IT WORKS
    ------------
    NJDOT specs use patterns like "as specified in 902.02" or "see 401.03.07".
    _XREF_RE finds all section numbers (XXX.XX or XXX.XX.XX) mentioned in the
    retrieved chunk text.

    For each referenced section not already in our results, we run a targeted
    keyword search using the section number as the query. This is fast because
    section numbers are highly specific — the keyword search finds the right
    chunk immediately with high confidence.

    WHY THIS HELPS
    --------------
    Without this, the LLM sees: "Provide HMA materials as specified in 902.02"
    but has no idea what 902.02 actually says. With this, we append the 902.02
    content so the LLM can actually answer material specification questions.

    Parameters
    ----------
    chunks : list of retrieved chunks (already in context)
    collection : collection filter to pass through
    already : set of section_ids already in results (to avoid duplicates)

    Returns
    -------
    List of additional chunks for the referenced sections (up to _MAX_XREFS).
    """
    # Collect all section numbers mentioned in retrieved chunk text
    referenced: List[str] = []
    seen_refs: Set[str] = set()
    for chunk in chunks:
        for match in _XREF_RE.finditer(chunk.get("content", "")):
            ref = match.group(1)
            if ref not in seen_refs and ref not in already:
                seen_refs.add(ref)
                referenced.append(ref)

    if not referenced:
        return []

    # Limit to avoid excessive API calls — take the most-mentioned refs first
    # (they appear more times in the text, so likely more important)
    from collections import Counter
    all_refs_raw = []
    for chunk in chunks:
        all_refs_raw.extend(_XREF_RE.findall(chunk.get("content", "")))
    freq = Counter(all_refs_raw)
    top_refs = [r for r, _ in freq.most_common() if r not in already][:_MAX_XREFS]

    if not top_refs:
        return []

    # For each cross-referenced section, search by exact section number.
    # Section numbers like "902.02" are highly specific — keyword search
    # finds the right chunk reliably and quickly.
    extra_chunks: List[Dict[str, Any]] = []
    seen_extra_sections: Set[str] = set(already)

    for ref in top_refs:
        try:
            results = _keyword.search(
                query=ref,
                collection=collection,
                match_count=2,  # top 2 chunks for that section
            )
            for r in results:
                sec = r.get("metadata", {}).get("section_id") or r.get("id")
                if sec not in seen_extra_sections:
                    seen_extra_sections.add(sec)
                    r["_xref_source"] = ref  # tag so prompt builder can note it
                    extra_chunks.append(r)
        except Exception as exc:
            logger.warning("Cross-ref fetch failed for section %r: %s", ref, exc)

    return extra_chunks


@router.post(
    "/query",
    response_model=QueryResponse,
    summary="Ask a question about NJDOT specifications",
    description=(
        "Runs hybrid retrieval (vector + BM25) → prompt assembly → "
        "gpt-4o-mini generation → citation validation and returns a "
        "structured JSON answer with source citations."
    ),
)
async def query_endpoint(request: QueryRequest) -> QueryResponse:
    """
    End-to-end RAG endpoint.

    Parameters
    ----------
    request : QueryRequest
        ``{"query": "...", "collection": "specs_2019" | null}``

    Returns
    -------
    QueryResponse
        ``{"answer": "...", "citations": [...], "query_type": "...",
           "response_time_ms": N}``

    Raises
    ------
    HTTPException 400
        If the query is empty (caught by Pydantic before reaching here).
    HTTPException 500
        If any step in the pipeline raises an unexpected exception.
    """
    t_start = time.time()

    # Pydantic already validated / stripped the query; double-check anyway.
    query      = request.query.strip()
    collection = request.collection

    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty or blank")

    # ── Query classification (for the response metadata field) ─────────────
    _v, _k, query_type = classify_query(query)

    amendments: list = []   # populated inside try; kept here for scope in response build

    try:
        # ── Step 1: Multi-query retrieval ─────────────────────────────────
        #
        # Instead of searching with just the original question, we generate
        # 2 rephrased variants and search with all 3 in parallel.
        #
        # WHY: Questions and spec text live in different regions of vector
        # space. By also searching with a "spec-style" rephrasing and a
        # keyword string, we land embeddings closer to where the answer text
        # actually lives. Chunks found by multiple variants score higher in
        # the merged results — that's the multi-query consensus signal.
        #
        chunks = _expander.expand_and_search(
            query, collection=collection, match_count=_RETRIEVE_K
        )

        # ── Step 1.5: Cross-reference follow-up ──────────────────────────
        #
        # Scan the retrieved chunks for section pointers like "902.02".
        # If found, fetch those target sections and append to context.
        #
        # WHY: The spec constantly says "as specified in 902.02" without
        # including the 902.02 content inline. Without this step the LLM
        # sees the pointer but not what it points to.
        #
        already_sections = {
            c.get("metadata", {}).get("section_id")
            for c in chunks
            if c.get("metadata", {}).get("section_id")
        }
        xref_chunks = _fetch_cross_ref_chunks(chunks, collection, already_sections)
        if xref_chunks:
            logger.debug("Appended %d cross-ref chunks", len(xref_chunks))
            chunks = chunks + xref_chunks

        # ── Step 1.6: BDC amendment lookup ───────────────────────────────
        section_ids = list({
            c["metadata"].get("section_id")
            for c in chunks
            if c.get("metadata", {}).get("section_id")
        })
        amendments = _bdc_matcher.get_amendments(section_ids)

        # ── Step 2: Prompt assembly ───────────────────────────────────────
        system_prompt, user_message = _builder.build(query, chunks, amendments=amendments)

        # ── Step 3: LLM generation ────────────────────────────────────────
        raw_response = _llm.complete(system_prompt, user_message)

        # ── Step 4: Parse + validate citations ───────────────────────────
        result = _serializer.serialize(raw_response, chunks)

    except Exception as exc:                              # noqa: BLE001
        logger.exception("Pipeline error for query=%r collection=%r", query, collection)
        raise HTTPException(
            status_code=500,
            detail=f"Pipeline error [{type(exc).__name__}]: {exc}",
        ) from exc

    # ── Build response ─────────────────────────────────────────────────────
    elapsed_ms = int((time.time() - t_start) * 1000)

    answer    = result.get("answer", "")
    raw_cites = result.get("citations") or []

    # If CitationSerializer fell back (parse_error), citations is already []
    citations: List[CitationItem] = [CitationItem(**c) for c in raw_cites]

    # Build BDC alert list (summary fields only — amendment_text goes to LLM, not client)
    bdc_alerts: List[BDCAlertItem] = [
        BDCAlertItem(
            bdc_id=a["bdc_id"],
            section_id=a["section_id"],
            effective_date=str(a.get("effective_date") or ""),
            subject=a.get("subject") or "",
            implementation_code=a.get("implementation_code") or "",
            change_type=a.get("change_type") or None,
        )
        for a in amendments
    ]

    return QueryResponse(
        answer=answer,
        citations=citations,
        query_type=query_type,
        response_time_ms=elapsed_ms,
        bdc_alerts=bdc_alerts,
    )


@router.post(
    "/debug",
    response_model=DebugResponse,
    summary="Debug retrieval pipeline internals",
    description=(
        "Runs hybrid retrieval with a larger candidate pool (``_DEBUG_K``) "
        "and returns per-chunk rank metadata (vector rank, keyword rank, RRF "
        "score, cleaned BM25 query) alongside the LLM answer.  "
        "Intended for development use only."
    ),
)
async def debug_endpoint(request: QueryRequest) -> DebugResponse:
    """
    End-to-end debug RAG endpoint.

    Runs the same pipeline as ``/api/query`` but with ``match_count=_DEBUG_K``
    and ``debug=True``, exposing per-chunk retrieval metadata so retrieval
    quality can be inspected.

    Parameters
    ----------
    request : QueryRequest
        ``{"query": "...", "collection": "specs_2019" | null}``

    Returns
    -------
    DebugResponse
        Full chunk metadata including vector/keyword ranks, RRF scores, the
        cleaned BM25 query string, query classification weights, and the LLM
        answer.

    Raises
    ------
    HTTPException 400
        If the query is empty.
    HTTPException 500
        If any step in the pipeline raises an unexpected exception.
    """
    t_start = time.time()

    query      = request.query.strip()
    collection = request.collection

    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty or blank")

    v_weight, k_weight, query_type = classify_query(query)

    try:
        # ── Step 1: Hybrid retrieval (debug mode, larger pool) ────────────
        loop = asyncio.get_event_loop()
        chunks, bm25_cleaned_query = await loop.run_in_executor(
            None,
            partial(_hybrid.search, query, collection, _DEBUG_K, True),
        )

        # ── Step 2: Prompt assembly ───────────────────────────────────────
        system_prompt, user_message = _builder.build(query, chunks)

        # ── Step 3: LLM generation ────────────────────────────────────────
        raw_response = _llm.complete(system_prompt, user_message)

        # ── Step 4: Parse + validate citations ───────────────────────────
        result = _serializer.serialize(raw_response, chunks)

    except Exception as exc:                              # noqa: BLE001
        logger.exception("Debug pipeline error for query=%r collection=%r", query, collection)
        raise HTTPException(
            status_code=500,
            detail=f"Debug pipeline error [{type(exc).__name__}]: {exc}",
        ) from exc

    elapsed_ms = int((time.time() - t_start) * 1000)
    answer     = result.get("answer", "")

    # ── Build per-chunk debug items ────────────────────────────────────────
    debug_chunks: List[DebugChunkItem] = []
    for chunk in chunks:
        meta    = chunk.get("metadata", {})
        content = chunk.get("content", "")
        preview = content[:300] + "..." if len(content) > 300 else content
        debug_chunks.append(DebugChunkItem(
            chunk_id      = chunk.get("id", ""),
            collection    = chunk.get("collection", ""),
            section_id    = meta.get("section_id", ""),
            section_title = meta.get("section_title", ""),
            doc           = meta.get("doc", ""),
            page_printed  = meta.get("page_printed"),
            rrf_score     = chunk.get("similarity", 0.0),
            vector_rank   = chunk.get("_vector_rank"),
            keyword_rank  = chunk.get("_keyword_rank"),
            content_preview = preview,
        ))

    return DebugResponse(
        query              = query,
        query_type         = query_type,
        vector_weight      = v_weight,
        keyword_weight     = k_weight,
        bm25_cleaned_query = bm25_cleaned_query,
        retrieve_k         = _DEBUG_K,
        chunks             = debug_chunks,
        answer             = answer,
        response_time_ms   = elapsed_ms,
    )
