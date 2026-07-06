"""Debug script: main RAG pipeline with full intermediate output.

Stages
------
 1  Query classification  (vector/keyword weights)
 2  Query expansion       (LLM generates 2 variants; shows full expansion prompt)
 3  Per-variant hybrid search  (vector_rank + keyword_rank per chunk)
 4  Multi-RRF merge       (final 12 chunks with query_count + dedup info)
 5  Cross-reference follow-up
 6  BDC amendments
 7  Prompt assembly
 8  LLM call
 9  Raw LLM response
10  Citation validation
11  Final answer

Usage:
    python -m debug.query "your question" [--collection COLL] [--show-prompt]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Set

import debug  # noqa: F401 — bootstraps sys.path
from app.config import config
from debug import bold, cyan, dim, green, hr, red, section, status_badge, subsection, yellow

from app.database import get_db
from app.retrieval.vector_search import VectorSearcher
from app.retrieval.bm25_search import KeywordSearcher
from app.retrieval.hybrid_ranker import HybridRanker, classify_query
from app.retrieval.query_expander import (
    QueryExpander,
    _EXPANSION_SYSTEM,   # type: ignore[attr-defined]
    _EXPANSION_FEW_SHOTS,  # type: ignore[attr-defined]
)
from app.retrieval.bdc_matcher import get_bdc_matcher
from app.generation.llm_client import LLMClient
from app.generation.prompt_builder import PromptBuilder
from app.generation.citation_serializer import CitationSerializer

# Cross-reference section number pattern (same as query.py)
_XREF_RE = re.compile(r'\b(\d{3}\.\d{2}(?:\.\d{2})?)\b')
_RETRIEVE_K = 12
_MAX_XREFS  = 3


def _fetch_xref_chunks(
    chunks:     List[Dict[str, Any]],
    keyword:    KeywordSearcher,
    collection: Optional[str],
    already:    Set[str],
) -> List[Dict[str, Any]]:
    """Replicated from query.py — fetch chunks for cross-referenced sections."""
    all_refs_raw = []
    for chunk in chunks:
        all_refs_raw.extend(_XREF_RE.findall(chunk.get("content", "")))
    freq = Counter(all_refs_raw)
    top_refs = [r for r, _ in freq.most_common() if r not in already][:_MAX_XREFS]

    extra: List[Dict[str, Any]] = []
    seen: Set[str] = set(already)
    for ref in top_refs:
        try:
            results = keyword.search(query=ref, collection=collection, match_count=2)
            for r in results:
                sec = r.get("metadata", {}).get("section_id") or r.get("id")
                if sec not in seen:
                    seen.add(sec)
                    r["_xref_source"] = ref
                    extra.append(r)
        except Exception as exc:
            print(yellow(f"    Warning: cross-ref fetch failed for {ref!r}: {exc}"))
    return extra


def run_query(query: str, collection: Optional[str], show_prompt: bool) -> None:

    db      = get_db()
    vector  = VectorSearcher(db_client=db)
    keyword = KeywordSearcher(db_client=db)
    hybrid  = HybridRanker(vector_searcher=vector, keyword_searcher=keyword)
    llm     = LLMClient()
    expander = QueryExpander(llm_client=llm, hybrid_ranker=hybrid)
    bdc_matcher = get_bdc_matcher()
    builder = PromptBuilder()
    serializer = CitationSerializer()

    # ── STAGE 1: Query classification ────────────────────────────────────────
    section("STAGE 1 — QUERY CLASSIFICATION")
    v_weight, k_weight, query_type = classify_query(query)
    print(f"  Query:       {bold(query)}")
    print(f"  Type:        {bold(query_type)}")
    print(f"  v_weight:    {v_weight}   (vector / semantic)")
    print(f"  k_weight:    {k_weight}   (keyword / BM25)")
    if collection:
        print(f"  Collection:  {collection}")
    else:
        print(f"  Collection:  {dim('all (no filter)')}")

    # ── STAGE 2: Query expansion ──────────────────────────────────────────────
    section("STAGE 2 — QUERY EXPANSION")

    # Show the expansion prompt sent to the LLM
    few_shot_text = "\n\n".join(
        f"Question: {q}\nOutput: {a}" for q, a in _EXPANSION_FEW_SHOTS
    )
    expansion_user_msg = f"{few_shot_text}\n\nQuestion: {query}\nOutput:"

    subsection("Expansion system prompt")
    print(f"  {dim(_EXPANSION_SYSTEM)}")
    subsection("Expansion user message (few-shots + query)")
    for line in expansion_user_msg.splitlines():
        print(f"  {dim(line)}")

    print()
    print(f"  Calling LLM for variants ... ", end="", flush=True)
    t_exp = time.time()
    variants = expander._generate_variants(query)
    print(green("✓") + f"  ({time.time() - t_exp:.2f}s)")

    print(f"\n  Variants generated ({len(variants)}):")
    labels = ["original", "spec_style", "keywords"]
    for i, v in enumerate(variants):
        label = labels[i] if i < len(labels) else f"variant_{i}"
        print(f"    [{bold(label)}]  {v}")

    # ── STAGE 3: Per-variant hybrid search ────────────────────────────────────
    section("STAGE 3 — PER-VARIANT HYBRID SEARCH")
    per_query_k = _RETRIEVE_K * 2
    all_ranked_lists: List[List[Dict[str, Any]]] = []

    for i, variant in enumerate(variants):
        label = labels[i] if i < len(labels) else f"variant_{i}"
        subsection(f'Variant [{label}]: "{variant}"')

        t_srch = time.time()
        result = hybrid.search(variant, collection=collection, match_count=per_query_k, debug=True)
        elapsed = time.time() - t_srch

        if isinstance(result, tuple):
            chunks_v, bm25_cleaned = result
            print(f"  BM25 cleaned query: {dim(repr(bm25_cleaned))}")
        else:
            chunks_v = result
        all_ranked_lists.append(chunks_v)

        v_wt, k_wt, lbl = classify_query(variant)
        print(f"  Results: {len(chunks_v)}  |  elapsed: {elapsed:.2f}s  |  type: {lbl}  weights: v={v_wt} k={k_wt}\n")

        for rank, chunk in enumerate(chunks_v, start=1):
            meta        = chunk.get("metadata", {})
            section_id  = meta.get("section_id", "—")
            section_ttl = meta.get("section_title", "—")
            doc         = meta.get("doc", "—")
            page_pdf    = meta.get("page_pdf", "—")
            page_print  = meta.get("page_printed", "—")
            ctx_sum     = meta.get("context_summary", "")
            rrf_score   = chunk.get("similarity", 0.0)
            v_rank      = chunk.get("_vector_rank")
            k_rank      = chunk.get("_keyword_rank")
            content     = chunk.get("content", "")

            print(f"  {'─' * 76}")
            print(f"  CHUNK [{rank:02d}]  section={cyan(section_id)}  rrf={green(f'{rrf_score:.6f}')}")
            print(f"    section_title: {bold(section_ttl)}")
            print(f"    doc:           {doc}")
            print(f"    page_pdf:      {page_pdf}  /  page_printed: {page_print}")
            print(f"    vector_rank:   {v_rank if v_rank is not None else dim('—')}  "
                  f"keyword_rank: {k_rank if k_rank is not None else dim('—')}")
            if ctx_sum:
                print(f"\n    context_summary:")
                print(f"      {dim(ctx_sum)}")
            print(f"\n    CONTENT (full):")
            for line in content.splitlines():
                print(f"      {line}")

    # ── STAGE 4: Multi-RRF merge ──────────────────────────────────────────────
    section("STAGE 4 — MULTI-RRF MERGE")
    merged = QueryExpander._multi_rrf_merge(all_ranked_lists, _RETRIEVE_K)

    # Show dedup info: how many unique chunk IDs across all variant results
    all_ids = [c["id"] for lst in all_ranked_lists for c in lst if c.get("id")]
    unique_ids = set(all_ids)
    print(f"  Total candidates across all variants: {len(all_ids)}  ({len(unique_ids)} unique chunks)")
    print(f"  After multi-RRF + section dedup: {len(merged)} chunks\n")

    for rank, chunk in enumerate(merged, start=1):
        meta        = chunk.get("metadata", {})
        section_id  = meta.get("section_id", "—")
        rrf_score   = chunk.get("similarity", 0.0)
        query_count = chunk.get("_query_count", "—")
        doc         = meta.get("doc", "—")
        print(f"  [{rank:02d}]  section={cyan(section_id)}  rrf={green(f'{rrf_score:.6f}')}  "
              f"query_count={bold(str(query_count))}  doc={doc}")

    # ── STAGE 5: Cross-reference follow-up ───────────────────────────────────
    section("STAGE 5 — CROSS-REFERENCE FOLLOW-UP")
    already_sections: Set[str] = {
        c.get("metadata", {}).get("section_id") or c["id"]
        for c in merged
        if c.get("id")
    }

    # Show all section refs found in chunk text
    all_found_refs: List[str] = []
    for chunk in merged:
        found = _XREF_RE.findall(chunk.get("content", ""))
        if found:
            section_id = chunk.get("metadata", {}).get("section_id", "?")
            print(f"  Chunk {cyan(section_id)} mentions: {', '.join(set(found))}")
            all_found_refs.extend(found)

    freq = Counter(all_found_refs)
    top_refs = [r for r, _ in freq.most_common() if r not in already_sections][:_MAX_XREFS]

    if top_refs:
        print(f"\n  Top cross-references to follow (not already in results): {top_refs}")
    else:
        print(f"\n  No new cross-references to follow.")

    xref_chunks = _fetch_xref_chunks(merged, keyword, collection, already_sections)
    print(f"  Cross-ref chunks fetched: {len(xref_chunks)}")

    for chunk in xref_chunks:
        meta       = chunk.get("metadata", {})
        section_id = meta.get("section_id", "—")
        xref_src   = chunk.get("_xref_source", "?")
        content    = chunk.get("content", "")
        print(f"\n  {'─' * 76}")
        print(f"  XREF CHUNK  fetched_for={cyan(xref_src)}  section={cyan(section_id)}")
        for line in content.splitlines():
            print(f"    {line}")

    # Combine
    final_chunks = merged + xref_chunks

    # ── STAGE 6: BDC amendments ───────────────────────────────────────────────
    section("STAGE 6 — BDC AMENDMENTS")
    section_ids = [
        c.get("metadata", {}).get("section_id")
        for c in final_chunks
        if c.get("metadata", {}).get("section_id")
    ]
    print(f"  Section IDs from retrieved chunks ({len(section_ids)}):")
    print(f"    {', '.join(sorted(set(section_ids)))}")

    amendments: List[Dict[str, Any]] = bdc_matcher.get_amendments(section_ids)

    if not amendments:
        print(f"\n  {dim('No BDC amendments found for these sections.')}")
    else:
        print(f"\n  Amendments found: {len(amendments)}")
        for amd in amendments:
            code_label = "URGENT" if amd.get("implementation_code") == "U" else "ROUTINE"
            print(f"\n  {'─' * 76}")
            print(f"  AMENDMENT: {bold(amd.get('bdc_id','?'))}  "
                  f"section={cyan(amd.get('section_id','?'))}  "
                  f"type={amd.get('change_type','?')}  "
                  f"code={yellow(code_label) if code_label == 'URGENT' else code_label}")
            print(f"    subject:   {amd.get('subject','—')}")
            print(f"    effective: {amd.get('effective_date','—')}")
            print(f"\n    TEXT (full):")
            for line in str(amd.get("amendment_text", "")).splitlines():
                print(f"      {line}")

    # ── STAGE 7: Prompt assembly ──────────────────────────────────────────────
    section("STAGE 7 — PROMPT ASSEMBLY")
    system_prompt, user_message = builder.build(query, final_chunks, amendments=amendments)

    print(f"  Chunks in prompt: {min(len(final_chunks), 8)} (PromptBuilder max=8)")
    print(f"  Amendments in prompt: {len(amendments)}")
    print(f"  System prompt length: {len(system_prompt):,} characters")
    print(f"  User message length:  {len(user_message):,} characters")

    if show_prompt:
        subsection("SYSTEM PROMPT (full)")
        print(system_prompt)
        subsection("USER MESSAGE (full)")
        print(user_message)

    # ── STAGE 8: LLM call ────────────────────────────────────────────────────
    section("STAGE 8 — LLM CALL")
    print(f"  Provider: {bold(llm.provider)}  Model: {bold(llm.model)}  temperature=0")
    t_llm = time.time()
    print(f"  Calling LLM ... ", end="", flush=True)
    raw_response = llm.complete(system_prompt, user_message)
    elapsed = time.time() - t_llm
    print(green("✓"))
    print(f"  Elapsed: {elapsed:.1f}s")

    # ── STAGE 9: Raw LLM response ─────────────────────────────────────────────
    section("STAGE 9 — RAW LLM RESPONSE")
    print(f"  Length: {len(raw_response):,} characters\n")
    print(raw_response)

    # ── STAGE 10: Citation validation ────────────────────────────────────────
    section("STAGE 10 — CITATION VALIDATION")
    validated = serializer.serialize(raw_response, final_chunks)

    raw_citations = []
    try:
        raw_json = json.loads(raw_response.strip().lstrip("```json").rstrip("```"))
        raw_citations = raw_json.get("citations", [])
    except Exception:
        pass

    citations = validated.get("citations", [])
    print(f"  LLM produced {len(raw_citations)} citation(s); validated: {len(citations)}")

    for i, (raw_cit, val_cit) in enumerate(
        zip(raw_citations, citations) if raw_citations else [(None, c) for c in citations],
        start=1,
    ):
        verified = val_cit.get("verified", False)
        match_icon = green("✓ VERIFIED") if verified else red("✗ UNVERIFIED")

        print(f"\n  {'─' * 76}")
        print(f"  CITATION [{i}]  {match_icon}")
        if raw_cit:
            print(f"    LLM claimed:   section={raw_cit.get('section','?')}  "
                  f"page_printed={raw_cit.get('page_printed','?')}  "
                  f"chunk_id={raw_cit.get('chunk_id','?')}")

        print(f"    After validate: section={val_cit.get('section','?')}  "
              f"page_printed={val_cit.get('page_printed','?')}  "
              f"page_pdf={val_cit.get('page_pdf','?')}  "
              f"chunk_id={val_cit.get('chunk_id','?')}")
        print(f"    document:       {val_cit.get('document','?')}")

    # ── STAGE 11: Final answer ────────────────────────────────────────────────
    section("STAGE 11 — FINAL ANSWER")
    answer = validated.get("answer", raw_response)
    print(f"\n  {bold('ANSWER:')}")
    for line in answer.splitlines():
        print(f"    {line}")

    print(f"\n  {bold('CITATIONS')} ({len(citations)}):")
    for i, cit in enumerate(citations, start=1):
        verified = cit.get("verified", False)
        icon     = green("✓") if verified else red("✗")
        print(f"    [{i}] {icon}  doc={cit.get('document','?')}  "
              f"section={cyan(cit.get('section','?'))}  "
              f"page_printed={cit.get('page_printed','?')}  "
              f"chunk_id={dim(str(cit.get('chunk_id','?'))[:24] + '...')}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Debug the main NJDOT RAG pipeline with full intermediate output.",
    )
    parser.add_argument("query",         help="Question to ask")
    parser.add_argument("--collection",  default=None,
                        choices=["specs_2019", "scheduling", "material_procs"],
                        help="Restrict retrieval to one collection (default: all)")
    parser.add_argument("--show-prompt", action="store_true",
                        help="Print full system + user prompts (can be very long)")
    args = parser.parse_args()

    run_query(
        query=args.query,
        collection=args.collection,
        show_prompt=args.show_prompt,
    )


if __name__ == "__main__":
    main()
