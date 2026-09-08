"""POST /api/review — Schedule compliance review endpoint.

Accepts a CPM schedule XER file, a narrative PDF, an optional Special
Provision PDF, an optional key map (key sheet) PDF, and an optional DBE Goal
Memo carrying the Engineer's Estimate. Seeds the schedule +
narrative into Neo4j under a fresh per-review ``project_id`` (multi-project
isolation — see ``graph_neo4j.tools``'s Cypher fencing and
``graph_neo4j.seed``'s composite-key ``MERGE`` writes) and runs the built-in
check catalog through ``app.compliance.eval_engine.evaluate_checks``: at
least one Pydantic-structured LLM call per check (GPT-4o primary, Claude
fallback via LangChain's ``.with_fallbacks()``), up to 4 when the grounding
judge retries (original + judge + retry + re-judge), replacing the old
single-mega-prompt + manual JSON parsing.

The Special Provision (if uploaded) is chunked, embedded, and persisted to
Supabase ``session_chunks`` (``app.ingestion.chunk_store.insert_session_chunks``)
so re-runs and chat can reuse it via ``retrieval_langchain.sp_retriever``
without re-parsing. The key map (if uploaded) gets one structured LLM
extraction (``ingestion.keymap_extractor``) whose result feeds the
utility-alignment comparison and the deterministic north/south-of-I-195
geography check (``app.compliance.geo``); its chunks go to
``session_chunks`` and its extraction JSON to
``review_projects.key_map_extraction`` (written by the frontend on initial
insert, this endpoint's rerun path, or the one-time backfill script) for
re-runs and chat. The DBE Goal Memo (if uploaded) is a scan, so page 1 is
transcribed by a vision call (``ingestion.estimate_extractor``); the
Engineer's Estimate it yields drives the deterministic Substantial-to-Final
completion-gap check (``app.compliance.cost``) and is likewise persisted to
Supabase (``session_chunks`` for chunks, ``review_projects.estimate_extraction``
for the extraction). Neo4j holds only the CPM schedule and Designer's
narrative for these reviews — none of the three document types above.

Returns the frontend's pre-existing JSON contract via ``_to_frontend_shape``
so ``DocumentReview.tsx`` needs no changes — only this adapter must track
frontend field-shape changes going forward.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import numpy as np
from fastapi import APIRouter, BackgroundTasks, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from yarl import URL as _YarlURL

from app.auth import user_id_from_token, user_id_from_token_optional
from app.compliance.catalog import BUILTIN_CHECKS, MANUAL_REVIEW_KEYS, CheckDef
from app.compliance.cost import CostGapResult, evaluate_cost_gap
from app.compliance.edq import EdqCoverageResult, evaluate_edq_coverage, match_edq_items_to_activities
from app.compliance.eval_engine import evaluate_checks
from app.compliance.geo import RegionResult, resolve_region
from app.config import config
from app.database import get_db
from app.graph_neo4j.seed import (
    seed_edq_items,
    seed_narrative,
    seed_schedule,
)
from app.ingestion.chunk_store import insert_session_chunks
from app.ingestion.edq_extractor import extract_edq_items
from app.ingestion.estimate_extractor import (
    EstimateExtraction,
    extract_estimate,
    render_estimate_facts,
)
from app.ingestion.keymap_extractor import (
    KeyMapExtraction,
    extract_key_map,
    extract_keymap_pages,
    render_keymap_facts,
)
from app.ingestion.pdf_parser import PDFParser
from app.ingestion.session_chunker import chunk_narrative, chunk_special_provision
from app.ingestion.utility_plan_extractor import extract_utility_plan, render_utility_plan_facts
from app.models import ReviewCheckResult, ReviewResponse
from app.neo4j_client import get_neo4j
from app.retrieval.vector_search import VectorSearcher
from app.retrieval_langchain.sp_retriever import retrieve_sp_chunks
from app.retrieval_langchain.utility_plan_retriever import retrieve_utility_plan_chunks
from app.scheduling import build_calendars, build_network, cross_check, run_cpm
# Re-exported for backward compatibility (session.py and debug scripts import
# these from app.api.review); canonical implementations live in app.scheduling.
from app.scheduling.xer_extract import (  # noqa: F401
    parse_xer_all,
    parse_xer_calendars,
    parse_xer_project,
    parse_xer_to_json,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["review"])

_ANTHROPIC_MODEL = "claude-sonnet-5"

_STORAGE_BUCKET = "review-files"

# Static, pre-ingested reference collections in Supabase's `chunks` table
# (see backend/scripts/ingest_specs.py) — project-independent, unlike the
# per-review Special Provision.
_SPEC_COLLECTION = "specs_2019"   # NJDOT Standard Specifications
_CSM_COLLECTION = "scheduling"    # Construction Scheduling Manual

# EvaluationSchema's Pass/Fail/Missing -> the frontend's existing lowercase enum.
_STATUS_MAP = {"Pass": "pass", "Fail": "fail", "Missing": "warning"}

# Fallback check_type by check_key, for `checks` payloads sent by a frontend
# that predates the field — see _parse_checks.
_BUILTIN_CHECK_TYPES = {c.check_key: c.check_type for c in BUILTIN_CHECKS}


# ── In-process review progress store ────────────────────────────────────────
# Dict is GIL-protected in CPython; safe for single-process FastAPI deploys
# (mirrors app.api.session's identically-shaped _progress store).

_review_progress: Dict[str, Dict[str, Any]] = {}


def _set_review_progress(project_id: str, **kwargs: Any) -> None:
    _review_progress[project_id] = {**_review_progress.get(project_id, {}), **kwargs}


# Mirrors the exact fixed prefix storage3's own request-building uses
# ("object", then the bucket name) before appending the caller-controlled
# path -- see backend/.venv's installed storage3/_sync/file_api.py's
# download()/upload()/_upload_or_update(), which all build
# ["object", self.id, *path_parts]. Anchoring to this exact shape (not an
# arbitrary placeholder) is required: a mismatched placeholder lets a
# leading '..' escape into the wrong number of fixed segments in the
# check's model vs. reality, letting a caller redirect the request to a
# DIFFERENT bucket entirely while still passing an ownership check scoped
# to their own user_id.
_PATH_RESOLUTION_BASE = _YarlURL(f"https://storage.invalid/object/{_STORAGE_BUCKET}")


def _relative_path_parts(path: str) -> tuple:
    """Mirrors storage3's own relative_path_to_parts (strips a leading
    absolute-path marker before returning yarl's parsed segments). Kept
    as a local copy rather than importing storage3's private
    `_sync.file_api` submodule -- this piece is 4 lines and trivial to
    keep in sync; the private import would be more fragile across
    storage3 version bumps than reimplementing this specific, tiny,
    documented behavior."""
    url = _YarlURL(path)
    if url.absolute or (url.parts and url.parts[0] == "/"):
        return url.parts[1:]
    return url.parts


def _validate_owned_path(path: str, user_id: str) -> None:
    """Raises 403 unless `path` resolves (after the SAME '.'/'..'
    resolution and percent-decoding storage3's own yarl-based request
    building performs) to a path anchored at "object/<_STORAGE_BUCKET>/
    <user_id>/..." -- not just "<user_id>/...". Checking only the
    trailing shape (as an earlier version of this function did) lets a
    leading '../' escape past the fixed "object"/bucket-name segments
    into a DIFFERENT bucket while still superficially "starting with the
    caller's own user_id" one level too shallow. Anchoring the full
    expected prefix closes that."""
    try:
        parts = _relative_path_parts(path)
        resolved = _PATH_RESOLUTION_BASE.joinpath(*parts).parts[1:]
    except Exception:
        # yarl can raise on more than just a doubly-encoded "%2Fx" segment
        # (e.g. a "//host"-prefixed path making it parse a bogus host, or
        # a segment that fails IDNA encoding) -- any parse failure here
        # means the input is malformed/adversarial, not a legitimate
        # value, so it's rejected the same way as a resolved-but-wrong
        # path rather than propagating as an unhandled 500.
        raise HTTPException(status_code=403, detail="Storage path does not belong to you.")
    if (
        len(resolved) < 4
        or resolved[0] != "object"
        or resolved[1] != _STORAGE_BUCKET
        or resolved[2] != user_id
    ):
        raise HTTPException(status_code=403, detail="Storage path does not belong to you.")


@router.get("/review/{project_id}/status", summary="Stream review progress via SSE")
async def review_status(project_id: str, token: Optional[str] = None) -> StreamingResponse:
    """Server-Sent Events stream of a review's progress.

    Each event is a JSON object: {status, message, result?}. status is one
    of "queued"/"running"/"ready"/"error". Stream closes once status is
    "ready" or "error". "result" (the full shaped review dict) is only
    present once status is "ready".

    Ownership check: a review with a real owner (a signed-in submission,
    not the anonymous flow) can only be streamed by that same user.
    EventSource cannot send a custom Authorization header, so the caller's
    token is passed as a query parameter instead -- the same JWT used
    everywhere else, just relayed differently because of that one browser
    API limitation. Anonymous reviews (no owner) stay openly readable,
    matching the rest of this app's "usable signed-out" design.

    If project_id isn't in the in-process store (e.g. after a server
    restart), falls back to Supabase's review_projects.review_result — so a
    review that actually finished before a restart still reports ready.
    """
    caller_user_id = user_id_from_token_optional(f"Bearer {token}") if token else None
    transient_error: Optional[str] = None

    if project_id in _review_progress:
        owner_user_id = _review_progress[project_id].get("user_id")
        if owner_user_id and owner_user_id != caller_user_id:
            raise HTTPException(status_code=403, detail="This review does not belong to you")
    else:
        try:
            db = get_db()
            rows = (
                db.table("review_projects").select("user_id, review_result")
                .eq("id", project_id).limit(1).execute().data
            ) or []
            row = rows[0] if rows else None
            owner_user_id = row.get("user_id") if row else None
            if owner_user_id and owner_user_id != caller_user_id:
                raise HTTPException(status_code=403, detail="This review does not belong to you")
            result = row.get("review_result") if row else None
            if result:
                _set_review_progress(project_id, status="ready", message="Review complete.", result=result, user_id=owner_user_id)
            else:
                _set_review_progress(project_id, status="error", message="Review not found.", user_id=owner_user_id)
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("DB check for review %s failed: %s", project_id, exc)
            transient_error = "Review not found."

    async def _generator():
        if transient_error is not None:
            yield f"data: {json.dumps({'status': 'error', 'message': transient_error})}\n\n"
            return
        while True:
            progress = _review_progress.get(
                project_id,
                {"status": "unknown", "message": "Review not found."},
            )
            yield f"data: {json.dumps(progress)}\n\n"
            if progress.get("status") in ("ready", "error"):
                break
            await asyncio.sleep(0.8)

    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── PDF helpers ──────────────────────────────────────────────────────────────

def _bytes_to_pdf_pages(raw: bytes) -> List[Dict[str, Any]]:
    """Write bytes to a temp file and extract pages via PDFParser."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        return PDFParser(tmp_path).extract_text()
    finally:
        os.unlink(tmp_path)


def _bytes_to_sp_chunks(raw: bytes) -> List[Dict[str, Any]]:
    """Chunk a Special Provision PDF with pdfplumber table extraction.

    Keeps the temp file alive while ``chunk_special_provision`` runs so
    pdfplumber can re-open it to extract tables as NL sentences — mirrors
    ``app.api.session``'s identically-named helper (kept separate to avoid a
    cross-router import). Unlike ``_bytes_to_pdf_pages``, which deletes the
    temp file before chunking even starts, this is required for
    ``chunk_special_provision(pages, pdf_path=...)``'s table-extraction path.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        pages = PDFParser(tmp_path).extract_text()
        return chunk_special_provision(pages, pdf_path=tmp_path)
    finally:
        os.unlink(tmp_path)


def _bytes_to_keymap_chunks(raw: bytes) -> List[Dict[str, Any]]:
    """Chunk the key map's PyMuPDF text (the same extraction source the
    structured LLM extraction reads — see ``ingestion.keymap_extractor``'s
    module docstring) for chat retrieval.

    ``pdf_path=None`` deliberately skips ``chunk_special_provision``'s
    pdfplumber table→NL path: on real key sheets pdfplumber's table detection
    fragments the CAD line work into garbage (one "table" per page containing
    the whole page in a single cell). The chunker hardcodes
    ``doc_type='special_provision'``, so it's rewritten here.
    """
    pages = extract_keymap_pages(raw)
    chunks = chunk_special_provision(pages, pdf_path=None)
    for c in chunks:
        c["metadata"]["doc_type"] = "key_map"
    return chunks


def _estimate_chunks_from_extraction(extraction: EstimateExtraction) -> List[Dict[str, Any]]:
    """Chat chunks for the estimate, built from the vision transcription.

    Unlike every other document here there is nothing to chunk from the PDF
    itself — DBE Goal Memos are scans with no text layer (see
    ``ingestion.estimate_extractor``), so the model's transcription *is* the
    text. Routed through the same chunker anyway so an unusually long
    transcription still splits; in practice page 1 yields a single chunk.
    """
    text = (extraction.page_transcription or "").strip()
    if not text:
        return []
    pages = [{"page_num": 1, "text": text, "char_count": len(text), "extractor": "vision"}]
    chunks = chunk_special_provision(pages, pdf_path=None)
    for c in chunks:
        c["metadata"]["doc_type"] = "estimate"
    return chunks


def _cosine(a: List[float], b: List[float]) -> float:
    va, vb = np.array(a), np.array(b)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    return float(np.dot(va, vb) / denom) if denom else 0.0


def _build_sp_search_fn(
    sp_chunks: List[Dict[str, Any]], sp_vectors: List[List[float]], embeddings: OpenAIEmbeddings,
) -> Optional[Callable[[str], str]]:
    """In-process cosine-ranked Special Provision search over already-chunked
    and already-embedded SP text.

    Takes precomputed ``sp_chunks``/``sp_vectors`` rather than raw bytes so
    the caller (``_run_review_pipeline``) can chunk/embed the SP PDF exactly
    once and share the result with both this search closure and the
    ``insert_session_chunks`` call that persists them to Supabase
    ``session_chunks`` — previously each computed its own chunks/embeddings
    independently, doubling SP embedding calls on every fresh review.
    """
    if not sp_chunks:
        return None
    texts = [c["content"] for c in sp_chunks]

    def _search(query: str, top_k: int = 5) -> str:
        q_vec = embeddings.embed_query(query)
        scored = sorted(zip(texts, sp_vectors), key=lambda tv: -_cosine(q_vec, tv[1]))
        top = [t for t, _ in scored[:top_k]]
        return "\n\n---\n\n".join(top) if top else "No matching Special Provision text found."

    return _search


def _build_sp_search_fn_from_supabase(
    db: Any, embeddings: OpenAIEmbeddings, project_id: str,
) -> Optional[Callable[[str], str]]:
    """Special Provision search backed by ``session_chunks`` -- the
    ``reseed=False`` fast path's equivalent of ``_build_sp_search_fn``,
    without re-parsing, re-chunking, or re-embedding the PDF. Reuses the same
    ``retrieve_sp_chunks`` chat already calls, so review and chat can never
    disagree about how SP retrieval works. Returns ``None`` if this project
    has no SP chunks (matches "no SP uploaded" behavior).
    """
    existing = (
        db.table("session_chunks").select("id", count="exact")
        .eq("session_id", project_id).eq("doc_type", "special_provision")
        .limit(1).execute()
    )
    if not existing.count:
        return None

    def _search(query: str) -> str:
        rows = retrieve_sp_chunks(db, embeddings.embed_query, project_id, query)
        if not rows:
            return "No matching Special Provision text found."
        return "\n\n---\n\n".join(r["content"] for r in rows)

    return _search


def _build_utility_plan_search_fn(
    chunks: List[Dict[str, Any]], vectors: List[List[float]], embeddings: OpenAIEmbeddings,
) -> Optional[Callable[[str], str]]:
    """In-process cosine-ranked Utility Agreement Plan search, mirroring
    ``_build_sp_search_fn`` exactly. ``chunks``/``vectors`` are pooled across
    every utility plan sheet uploaded for this review (one per utility), so a
    check searches all of them together."""
    if not chunks:
        return None
    texts = [c["content"] for c in chunks]

    def _search(query: str, top_k: int = 5) -> str:
        q_vec = embeddings.embed_query(query)
        scored = sorted(zip(texts, vectors), key=lambda tv: -_cosine(q_vec, tv[1]))
        top = [t for t, _ in scored[:top_k]]
        return "\n\n---\n\n".join(top) if top else "No matching Utility Agreement Plan text found."

    return _search


def _build_utility_plan_search_fn_from_supabase(
    db: Any, embeddings: OpenAIEmbeddings, project_id: str,
) -> Optional[Callable[[str], str]]:
    """Utility Agreement Plan search backed by ``session_chunks`` -- the
    ``reseed=False`` fast path's equivalent of ``_build_utility_plan_search_fn``.
    Reuses the same ``retrieve_utility_plan_chunks`` chat already calls.
    Returns ``None`` if this project has no utility plan chunks (matches "none
    uploaded" behavior -- this source is optional, see eval_engine)."""
    existing = (
        db.table("session_chunks").select("id", count="exact")
        .eq("session_id", project_id).eq("doc_type", "utility_plan")
        .limit(1).execute()
    )
    if not existing.count:
        return None

    def _search(query: str) -> str:
        rows = retrieve_utility_plan_chunks(db, embeddings.embed_query, project_id, query)
        if not rows:
            return "No matching Utility Agreement Plan text found."
        return "\n\n---\n\n".join(r["content"] for r in rows)

    return _search


def _read_keymap_extraction_from_supabase(db: Any, project_id: str) -> Optional[KeyMapExtraction]:
    """Rehydrate the key map extraction from
    ``review_projects.key_map_extraction`` -- the ``reseed=False`` fast
    path's way to skip re-running the extraction LLM call on re-runs.
    Returns ``None`` if this project never had a key map, or the column is
    still unpopulated (pre-migration project awaiting backfill)."""
    rows = (
        db.table("review_projects").select("key_map_extraction")
        .eq("id", project_id).limit(1).execute()
    ).data
    if not rows or not rows[0].get("key_map_extraction"):
        return None
    try:
        return KeyMapExtraction.model_validate(rows[0]["key_map_extraction"])
    except Exception:
        logger.exception("Failed to parse persisted key map extraction for project_id=%s", project_id)
        return None


def _extract_and_store_keymap(
    db: Any,
    embeddings: OpenAIEmbeddings,
    llm: Any,
    keymap_bytes: bytes,
    project_id: str,
    user_id: Optional[str],
) -> Optional[KeyMapExtraction]:
    """Structured-extract the key map, then persist chunks to Supabase
    ``session_chunks`` so re-runs and chat can reuse them without
    re-parsing. Returns the extraction (``None`` for scanned/unextractable
    sheets -- the review proceeds and keymap checks go "Missing").

    The extraction JSON itself is NOT persisted here -- it's already in this
    function's return value, which the caller includes in the API response
    (see ``_run_review_pipeline``'s ``shaped["key_map"]``); the frontend
    writes it onto ``review_projects.key_map_extraction`` from there.
    """
    keymap_extraction = extract_key_map(keymap_bytes, llm, project_id=project_id, user_id=user_id)
    km_chunks = _bytes_to_keymap_chunks(keymap_bytes)
    if km_chunks:
        km_vectors = embeddings.embed_documents([c["content"] for c in km_chunks])
        merged = [{**c, "embedding": v} for c, v in zip(km_chunks, km_vectors)]
        insert_session_chunks(db, project_id, merged)
    return keymap_extraction


def _read_estimate_extraction_from_supabase(db: Any, project_id: str) -> Optional[EstimateExtraction]:
    """Rehydrate the estimate extraction from
    ``review_projects.estimate_extraction`` -- the ``reseed=False`` fast
    path's way to skip re-running the vision call (the most expensive single
    call in the pipeline). ``None`` when the column is unset."""
    rows = (
        db.table("review_projects").select("estimate_extraction")
        .eq("id", project_id).limit(1).execute()
    ).data
    if not rows or not rows[0].get("estimate_extraction"):
        return None
    try:
        return EstimateExtraction.model_validate(rows[0]["estimate_extraction"])
    except Exception:
        logger.exception("Failed to parse persisted estimate extraction for project_id=%s", project_id)
        return None


def _extract_and_store_estimate(
    db: Any,
    embeddings: OpenAIEmbeddings,
    llm: Any,
    estimate_bytes: bytes,
    project_id: str,
    user_id: Optional[str],
) -> Optional[EstimateExtraction]:
    """Vision-extract page 1 of the estimate, then persist the transcription
    chunk to Supabase ``session_chunks``. ``None`` for unreadable pages --
    the review proceeds and the gap check reports "Missing".

    Extraction JSON is not persisted here -- see
    ``_extract_and_store_keymap``'s docstring for why.
    """
    estimate_extraction = extract_estimate(estimate_bytes, llm, project_id=project_id, user_id=user_id)
    if estimate_extraction is None:
        return None
    est_chunks = _estimate_chunks_from_extraction(estimate_extraction)
    if est_chunks:
        est_vectors = embeddings.embed_documents([c["content"] for c in est_chunks])
        merged = [{**c, "embedding": v} for c, v in zip(est_chunks, est_vectors)]
        insert_session_chunks(db, project_id, merged)
    return estimate_extraction


def _seed_edq_items_if_needed(
    graph: Any,
    llm: Any,
    embeddings: OpenAIEmbeddings,
    estimate_bytes: Optional[bytes],
    project_id: str,
    user_id: Optional[str],
) -> None:
    """Extract -> match -> seed EDQ items into Neo4j, but only the first
    time this project needs it.

    Gated on "does EdqItem data already exist for this projectId" (a cheap
    count() query) -- this makes reruns of an already-seeded project a
    no-op (evaluate_edq_coverage reads the durable graph directly), and
    self-heals projects reviewed before this feature existed.

    No ``reseed`` parameter: every fresh upload (``POST /api/review``)
    already mints a brand-new ``project_id`` (see ``review_endpoint``), and
    ``POST /api/review/{project_id}/rerun`` always re-downloads the exact
    same stored document bytes -- there is no real path where an existing
    project_id needs to be re-matched against revised source documents, so
    a reseed/delete branch here would be dead code with no way to exercise
    it against real traffic.
    """
    existing = graph.query(
        "MATCH (e:EdqItem {projectId: $pid}) RETURN count(e) AS c LIMIT 1",
        params={"pid": project_id},
    )
    if existing and existing[0]["c"]:
        return
    if not estimate_bytes:
        return
    extraction = extract_edq_items(estimate_bytes, llm, project_id=project_id, user_id=user_id)
    if extraction is None or not extraction.items:
        return
    activities = graph.query(
        "MATCH (a:Activity {projectId: $pid}) "
        "RETURN a.taskId AS taskId, a.name AS name, a.wbsPath AS wbsPath",
        params={"pid": project_id},
    ) or []
    if not activities:
        return
    items = [{
        "id": f"edq:{i}", "jobId": it.job_id, "category": it.category,
        "itemDescription": it.item_description, "estimatedQuantity": it.estimated_quantity,
        "unit": it.unit, "sourcePage": it.source_page,
    } for i, it in enumerate(extraction.items)]
    matches = match_edq_items_to_activities(
        items, activities, llm, embeddings, project_id=project_id, user_id=user_id)
    if matches is None:
        return  # matching failed -- don't seed a false "all uncovered" state; retry next run
    seed_edq_items(graph, items, matches, project_id=project_id)


def _build_static_doc_search_fn(collection: str, match_count: int = 5) -> Optional[Callable[[str], str]]:
    """Search-function factory for a static, pre-ingested reference
    collection (Standard Specifications or the Construction Scheduling
    Manual). Unlike ``_build_sp_search_fn*``, this doesn't depend on any
    per-review upload — the collection is embedded once, system-wide, by
    ``backend/scripts/ingest_specs.py`` — so it's built unconditionally for
    every review. Returns ``None`` only if the searcher itself can't be
    constructed (e.g. missing OpenAI key), so a check requesting this source
    degrades to "Missing" instead of crashing the whole review.
    """
    try:
        searcher = VectorSearcher()
    except Exception:
        logger.exception("Failed to initialize VectorSearcher for collection=%s", collection)
        return None

    def _search(query: str) -> str:
        try:
            results = searcher.search(query, collection=collection, match_count=match_count)
        except Exception:
            logger.exception("Static-doc search failed for collection=%s", collection)
            return "No matching reference text found (search error)."
        if not results:
            return "No matching reference text found."
        return "\n\n---\n\n".join(r["content"] for r in results)

    return _search


def _read_project_summary(graph: Any, project_id: str) -> tuple[str, int]:
    """Read ``project_name``/``duration_days`` back from the ``Project`` node
    — used by the ``reseed=False`` fast path instead of re-parsing the XER.
    """
    rows = graph.query(
        "MATCH (p:Project {projectId: $pid}) "
        "RETURN p.projectName AS projectName, p.durationDays AS durationDays LIMIT 1",
        params={"pid": project_id},
    )
    if not rows:
        return "Unknown", 0
    row = rows[0]
    return row.get("projectName") or "Unknown", row.get("durationDays") or 0


# ── Checklist selection ────────────────────────────────────────────────────────

def _parse_checks(raw: Optional[str]) -> Optional[List[CheckDef]]:
    """Parse the ``checks`` form field into a ``CheckDef`` list.

    ``raw`` is a JSON array of ``{check_key, category, name, instruction,
    check_type, source_files}`` (the frontend's effective checklist —
    built-ins plus any user customizations). Returns ``None`` when ``raw`` is absent (caller
    falls back to the full ``BUILTIN_CHECKS`` catalog, matching pre-Feature-1
    behavior). Raises ``HTTPException(400)`` on malformed JSON, a non-array
    payload, an empty array ("no checks selected"), or a check missing a
    required field.
    """
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="checks must be valid JSON") from exc
    if not isinstance(parsed, list):
        raise HTTPException(status_code=400, detail="checks must be a JSON array")
    if len(parsed) == 0:
        raise HTTPException(status_code=400, detail="No checks selected for review.")

    checks: List[CheckDef] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"checks[{i}] must be an object")
        try:
            checks.append(CheckDef(
                check_key=item["check_key"],
                category=item["category"],
                name=item["name"],
                instruction=item.get("instruction", ""),
                # Catalog-lookup fallback covers payloads from frontends that
                # predate check_type in CheckSpec — without it a deterministic
                # check would silently run as an ordinary LLM call.
                check_type=(item.get("check_type")
                            or _BUILTIN_CHECK_TYPES.get(item["check_key"], "llm")),
                source_files=item.get("source_files") or ["schedule"],
            ))
        except KeyError as exc:
            raise HTTPException(
                status_code=400, detail=f"checks[{i}] missing required field: {exc}",
            ) from exc
    return checks


# ── Response shaping ──────────────────────────────────────────────────────────

def _summarize(results: List[ReviewCheckResult]) -> Dict[str, int]:
    return {
        "passed": sum(1 for r in results if r.status == "Pass"),
        "warnings": sum(1 for r in results if r.status == "Missing"),
        "failed": sum(1 for r in results if r.status == "Fail"),
        "manual_review": sum(1 for r in results if r.id in MANUAL_REVIEW_KEYS),
    }


def _to_frontend_shape(response: ReviewResponse) -> dict:
    """Map the internal Pass/Fail/Missing + evidence/source shape onto the
    frontend's existing pass/warning/fail + reasoning/finding/evidence shape.

    ``schedule_file_path``/``narrative_pdf_path``/``special_provision_pdf_path``
    are deliberately NOT included here — this function runs inside
    ``_run_review_pipeline``, which is upload-agnostic (see its docstring);
    callers that know the Storage paths merge them into this dict themselves.
    """
    return {
        "project_id": response.project_id,
        "project_name": response.project_name,
        "project_duration_days": response.project_duration_days,
        "model_used": response.model_used,
        "summary": response.summary,
        "checks": [
            {
                "id": c.id,
                "category": c.category,
                "name": c.name,
                "reasoning": f"Source: {c.source}",
                "status": _STATUS_MAP.get(c.status, "warning"),
                "finding": c.evidence,
                "evidence": c.evidence,
            }
            for c in response.checks
        ],
        "manual_review_items": response.manual_review_items,
    }


def _run_review_pipeline(
    schedule_bytes: bytes,
    narrative_bytes: bytes,
    sp_bytes: Optional[bytes],
    keymap_bytes: Optional[bytes],
    estimate_bytes: Optional[bytes],
    selected_checks: Optional[List[CheckDef]],
    project_id: str,
    reseed: bool = True,
    user_id: Optional[str] = None,
    utility_plan_bytes_list: Optional[List[bytes]] = None,
) -> dict:
    """Parse -> CPM -> seed Neo4j -> evaluate the checklist -> frontend shape.

    Shared by the initial upload endpoint and the rerun endpoint — the only
    difference between them is where the byte blobs come from (a fresh
    multipart upload vs. downloaded from Storage). ``project_id`` fences every
    Neo4j node written/read this call to one project — see graph_neo4j's
    multi-project isolation design (no wipe step: MERGE-based writes are
    idempotent, and a project's schedule bytes never change between re-runs).

    ``reseed=False`` (re-run) skips re-parsing the XER, re-running CPM, and
    re-parsing/re-chunking/re-embedding the narrative and Special Provision
    PDFs when this project was already seeded — those bytes are identical
    to the original run, so redoing ingestion would just recreate data
    that's already in Neo4j (and re-spend OpenAI embedding calls for
    nothing). Falls back to a full reseed if no existing data is found
    (self-healing for an edge case where the graph never got seeded).
    """
    start_time = datetime.now(timezone.utc)
    logger.info("=== Review started at %s ===", start_time.isoformat(timespec="seconds"))

    graph = get_neo4j()
    db = get_db()
    embeddings = OpenAIEmbeddings(model=config.EMBEDDING_MODEL, api_key=config.OPENAI_API_KEY)

    # Built before the seed branches (not just before check evaluation)
    # because the key map's structured extraction call needs it.
    primary = ChatOpenAI(model=config.CHAT_MODEL, temperature=0, api_key=config.OPENAI_API_KEY)
    fallback = ChatAnthropic(model=_ANTHROPIC_MODEL, anthropic_api_key=config.ANTHROPIC_API_KEY)
    llm = primary.with_fallbacks([fallback])

    if not reseed:
        existing = graph.query(
            "MATCH (a:Activity {projectId: $pid}) RETURN count(a) AS c LIMIT 1",
            params={"pid": project_id},
        )
        reseed = not existing or not existing[0]["c"]
        if reseed:
            logger.warning(
                "_run_review_pipeline: project_id=%s has no seeded graph data; "
                "falling back to a full reseed", project_id,
            )

    if reseed:
        # ── Parse XER -> CPM -> crosscheck ──────────────────────────────────
        xer_text = schedule_bytes.decode("utf-8", errors="ignore")
        parsed = parse_xer_all(xer_text)
        activities = parsed["activities"]
        calendars = parsed["calendars"]
        project = parsed["project"]

        # Non-fatal: on failure the review proceeds without computed CPM values.
        cpm = xcheck = None
        try:
            cpm = run_cpm(build_network(activities), build_calendars(calendars), project)
            xcheck = cross_check(activities, cpm)
        except Exception:
            logger.exception("CPM computation failed; review proceeds without computed values")

        # ── Seed Neo4j (schedule + narrative + SP), fenced to this project_id ──
        _set_review_progress(project_id, status="running", message="Seeding schedule graph…")
        seed_schedule(graph, activities, calendars, cpm, xcheck, project, project_id=project_id)

        _set_review_progress(project_id, status="running", message="Seeding narrative graph…")
        narrative_pages = _bytes_to_pdf_pages(narrative_bytes)
        nar_chunks = chunk_narrative(narrative_pages)
        nar_vectors = embeddings.embed_documents([c["content"] for c in nar_chunks]) if nar_chunks else []
        # llm=None: entity extraction (Commitment/Permit/... nodes) is a Document
        # Q&A enrichment, not needed for the checklist itself — the full narrative
        # text (seeded regardless) is what evaluate_checks reads.
        seed_narrative(graph, nar_chunks, nar_vectors, activities, project_id=project_id, llm=None)

        # Chunk + embed the SP PDF (with table extraction) exactly once, and
        # share the result between the immediate compliance-check search
        # closure and Supabase persistence — also persisted so a later
        # re-run can reuse them via retrieve_sp_chunks instead of recreating
        # them here.
        sp_search_fn = None
        if sp_bytes:
            _set_review_progress(project_id, status="running", message="Processing special provision…")
            sp_chunks = _bytes_to_sp_chunks(sp_bytes)
            if sp_chunks:
                sp_vectors = embeddings.embed_documents([c["content"] for c in sp_chunks])
                merged_sp = [{**c, "embedding": v} for c, v in zip(sp_chunks, sp_vectors)]
                insert_session_chunks(db, project_id, merged_sp)
                sp_search_fn = _build_sp_search_fn(sp_chunks, sp_vectors, embeddings)

        keymap_extraction: Optional[KeyMapExtraction] = None
        if keymap_bytes:
            _set_review_progress(project_id, status="running", message="Extracting key map…")
            keymap_extraction = _extract_and_store_keymap(
                db, embeddings, llm, keymap_bytes, project_id, user_id,
            )

        estimate_extraction: Optional[EstimateExtraction] = None
        if estimate_bytes:
            _set_review_progress(project_id, status="running", message="Extracting engineer's estimate…")
            estimate_extraction = _extract_and_store_estimate(
                db, embeddings, llm, estimate_bytes, project_id, user_id,
            )

        # Utility Agreement Plan sheets (one per utility -- gas, water/sewer,
        # electric, telecom, ...). Mirrors the SP block: vision-extract each,
        # embed once, share between the immediate search closure and Supabase
        # persistence. Optional cross-reference evidence -- see eval_engine's
        # missing_sources handling for why an absent one never blocks a check.
        utility_plan_search_fn = None
        if utility_plan_bytes_list:
            _set_review_progress(project_id, status="running", message="Processing utility plans…")
            utility_plan_chunks = []
            for b in utility_plan_bytes_list:
                extraction = extract_utility_plan(b, llm, project_id=project_id, user_id=user_id)
                if extraction is not None:
                    utility_plan_chunks.append({
                        "content": render_utility_plan_facts(extraction),
                        "metadata": {"doc_type": "utility_plan", "utility_owner": extraction.utility_owner},
                    })
            if utility_plan_chunks:
                utility_plan_vectors = embeddings.embed_documents(
                    [c["content"] for c in utility_plan_chunks])
                merged_utility_plans = [
                    {**c, "embedding": v} for c, v in zip(utility_plan_chunks, utility_plan_vectors)]
                insert_session_chunks(db, project_id, merged_utility_plans)
                utility_plan_search_fn = _build_utility_plan_search_fn(
                    utility_plan_chunks, utility_plan_vectors, embeddings)

        project_name = (project or {}).get("project_name") or "Unknown"
        duration_days = 0
        if cpm is not None and cpm.data_date and cpm.project_finish:
            duration_days = (cpm.project_finish - cpm.data_date).days
    else:
        # ── Fast path: project already seeded — reuse existing chunks ───────
        _set_review_progress(project_id, status="running", message="Loading saved project data…")
        project_name, duration_days = _read_project_summary(graph, project_id)
        sp_search_fn = _build_sp_search_fn_from_supabase(db, embeddings, project_id)
        if sp_search_fn is None and sp_bytes:
            # SP chunks stored but never persisted to Supabase (e.g. a
            # pre-migration project awaiting backfill, or a partial prior
            # run) — chunk/embed/store now, mirroring the keymap/estimate
            # self-healing below. Without this, a genuinely-uploaded SP
            # would be misreported as "not uploaded for this review" by
            # eval_engine's missing-sources check.
            sp_chunks = _bytes_to_sp_chunks(sp_bytes)
            if sp_chunks:
                sp_vectors = embeddings.embed_documents([c["content"] for c in sp_chunks])
                merged_sp = [{**c, "embedding": v} for c, v in zip(sp_chunks, sp_vectors)]
                insert_session_chunks(db, project_id, merged_sp)
                sp_search_fn = _build_sp_search_fn(sp_chunks, sp_vectors, embeddings)
        # No self-heal here: unlike SP, utility plan bytes are never uploaded
        # to Storage, so a rerun has no way to re-fetch them if missing --
        # only whatever was chunked at the original submission is available.
        utility_plan_search_fn = _build_utility_plan_search_fn_from_supabase(db, embeddings, project_id)
        keymap_extraction = _read_keymap_extraction_from_supabase(db, project_id)
        if keymap_extraction is None and keymap_bytes:
            # Key map stored but never persisted (e.g. a pre-key-map-feature
            # project re-run after the file landed in Storage) — extract and
            # store now, mirroring the reseed self-healing above.
            keymap_extraction = _extract_and_store_keymap(
                db, embeddings, llm, keymap_bytes, project_id, user_id,
            )
        estimate_extraction = _read_estimate_extraction_from_supabase(db, project_id)
        if estimate_extraction is None and estimate_bytes:
            estimate_extraction = _extract_and_store_estimate(
                db, embeddings, llm, estimate_bytes, project_id, user_id,
            )

    _set_review_progress(project_id, status="running", message="Preparing compliance checklist…")
    _seed_edq_items_if_needed(graph, llm, embeddings, estimate_bytes, project_id, user_id)

    # Geo and the cost gap are recomputed on every run (both are pure
    # in-process computations) so polyline/parser/threshold fixes apply to
    # re-runs of already-extracted projects.
    keymap_geo: Optional[RegionResult] = None
    keymap_facts: Optional[str] = None
    if keymap_extraction is not None:
        keymap_geo = resolve_region(keymap_extraction)
        keymap_facts = render_keymap_facts(keymap_extraction, keymap_geo)

    cost_gap: Optional[CostGapResult] = None
    estimate_facts: Optional[str] = None
    if estimate_extraction is not None:
        cost_gap = evaluate_cost_gap(graph, project_id, estimate_extraction)
        estimate_facts = render_estimate_facts(estimate_extraction, cost_gap)

    # EDQ coverage is a cheap read of the already-seeded graph (seeded just
    # above, once per project) -- recomputed every run like geo/cost_gap so
    # it reflects the current graph state.
    edq_coverage: Optional[EdqCoverageResult] = evaluate_edq_coverage(graph, project_id)

    # ── Evaluate the checklist ───────────────────────────────────────────────────
    # Static reference collections — independent of reseed/fast-path above,
    # since they're ingested once system-wide, not per review.
    spec_search_fn = _build_static_doc_search_fn(_SPEC_COLLECTION)
    csm_search_fn = _build_static_doc_search_fn(_CSM_COLLECTION)

    total_checks = len(selected_checks or BUILTIN_CHECKS)
    _set_review_progress(
        project_id, status="running",
        message=f"Running compliance checks (0/{total_checks})…",
    )
    try:
        check_results = evaluate_checks(
            selected_checks or BUILTIN_CHECKS, graph, llm,
            sp_search_fn=sp_search_fn, spec_search_fn=spec_search_fn, csm_search_fn=csm_search_fn,
            keymap_facts=keymap_facts, keymap_geo=keymap_geo,
            estimate_facts=estimate_facts, cost_gap=cost_gap,
            edq_coverage=edq_coverage,
            utility_plan_search_fn=utility_plan_search_fn,
            project_id=project_id, user_id=user_id,
            on_progress=lambda done, total: _set_review_progress(
                project_id, status="running",
                message=f"Running compliance checks ({done}/{total})…",
            ),
        )
    except Exception as exc:
        logger.exception("Compliance evaluation failed")
        raise HTTPException(status_code=502, detail="Compliance evaluation failed.") from exc

    response = ReviewResponse(
        project_id=project_id,
        project_name=project_name,
        project_duration_days=duration_days,
        summary=_summarize(check_results),
        checks=check_results,
        manual_review_items=[c.name for c in check_results if c.id in MANUAL_REVIEW_KEYS],
        # .with_fallbacks() resolves per-call; a single top-level label can't
        # capture "some checks fell back to Claude" — reporting the primary
        # model here is a reasonable simplification for this metadata field.
        model_used=config.CHAT_MODEL,
    )

    end_time = datetime.now(timezone.utc)
    elapsed = (end_time - start_time).total_seconds()
    logger.info(
        "=== Review finished at %s (%.1fs elapsed) | started %s | reseed=%s ===",
        end_time.isoformat(timespec="seconds"), elapsed, start_time.isoformat(timespec="seconds"), reseed,
    )

    shaped = _to_frontend_shape(response)
    # Full key map extraction + deterministic region result, persisted with
    # the rest of the response into review_projects.review_result — the
    # sheet index and misc fields aren't consumed anywhere yet, but this is
    # what keeps them available later without a schema change.
    shaped["key_map"] = (
        {
            "extraction": keymap_extraction.model_dump() if keymap_extraction else None,
            "region": keymap_geo.as_dict() if keymap_geo else None,
        }
        if (keymap_bytes or keymap_extraction is not None)
        else None
    )
    # Same rationale as key_map: the identifiers and transcription aren't
    # consumed anywhere yet, but persisting them here keeps them available
    # without a schema change.
    shaped["estimate"] = (
        {
            "extraction": estimate_extraction.model_dump() if estimate_extraction else None,
            "cost_gap": cost_gap.as_dict() if cost_gap else None,
        }
        if (estimate_bytes or estimate_extraction is not None)
        else None
    )
    shaped["edq"] = {"coverage": edq_coverage.as_dict() if edq_coverage else None}
    return shaped


def _run_review_pipeline_background(
    project_id: str,
    schedule_bytes: bytes,
    narrative_bytes: bytes,
    sp_bytes: Optional[bytes],
    keymap_bytes: Optional[bytes],
    estimate_bytes: Optional[bytes],
    selected_checks: Optional[List[CheckDef]],
    user_id: Optional[str],
    utility_plan_bytes_list: Optional[List[bytes]],
    schedule_path: Optional[str],
    narrative_path: Optional[str],
    sp_path: Optional[str],
    keymap_path: Optional[str],
    estimate_path: Optional[str],
) -> None:
    """Runs _run_review_pipeline for the initial upload endpoint and stores
    the outcome in _review_progress -- the actual work behind the
    "processing" response review_endpoint returns immediately.

    Must catch every exception itself: raised inside a BackgroundTasks
    callback (not an actual request), FastAPI has nothing to return an
    HTTPException to -- an uncaught one here would just be logged, leaving
    _review_progress stuck at "queued"/"running" forever.
    """
    _set_review_progress(
        project_id, status="running",
        message="Running compliance review — this can take several minutes…",
    )
    try:
        result = _run_review_pipeline(
            schedule_bytes, narrative_bytes, sp_bytes, keymap_bytes, estimate_bytes,
            selected_checks, project_id, user_id=user_id,
            utility_plan_bytes_list=utility_plan_bytes_list,
        )
        result["schedule_file_path"] = schedule_path
        result["narrative_pdf_path"] = narrative_path
        result["special_provision_pdf_path"] = sp_path
        result["key_map_pdf_path"] = keymap_path
        result["estimate_pdf_path"] = estimate_path
        _set_review_progress(project_id, status="ready", message="Review complete.", result=result)
    except HTTPException as exc:
        logger.exception("Background review failed for project_id=%s", project_id)
        _set_review_progress(project_id, status="error", message=str(exc.detail))
    except Exception as exc:
        logger.exception("Background review failed for project_id=%s", project_id)
        _set_review_progress(project_id, status="error", message="An unexpected error occurred while running the review.")


@router.post(
    "/review",
    summary="Schedule compliance review",
    description=(
        "Accepts a CPM schedule XER file, a narrative PDF, an optional "
        "Special Provision PDF, an optional key map (key sheet) PDF, and an "
        "optional DBE Goal Memo / Engineer's Estimate PDF. "
        "Runs the NJDOT compliance checklist against a Neo4j-backed "
        "knowledge graph (GPT-4o primary, Claude fallback) and returns a "
        "structured JSON report."
    ),
)
async def review_endpoint(
    background_tasks: BackgroundTasks,
    schedule_file: Optional[UploadFile] = File(
        None, description="CPM schedule XER file. Omit if schedule_file_path is given."),
    narrative_pdf: Optional[UploadFile] = File(
        None, description="Project narrative PDF. Omit if narrative_pdf_path is given."),
    special_provision_pdf: Optional[UploadFile] = File(
        None, description="Optional Special Provision PDF"),
    key_map_pdf: Optional[UploadFile] = File(
        None, description="Optional NJDOT key map (key sheet) PDF"),
    estimate_pdf: Optional[UploadFile] = File(
        None, description="Optional DBE Goal Memo / Engineer's Estimate PDF (page 1 is read)"),
    utility_plan_pdfs: Optional[List[UploadFile]] = File(
        None, description="Optional Utility Agreement Plan sheet PDFs -- one per utility "
                           "(gas, water/sewer, electric, telecom, ...)."),
    schedule_file_path: Optional[str] = Form(
        None, description="Storage path of a schedule file already uploaded directly to "
                           "Supabase Storage by a signed-in client, in place of schedule_file "
                           "-- bypasses Vercel's fixed 4.5MB serverless request-body limit."),
    narrative_pdf_path: Optional[str] = Form(
        None, description="Storage path in place of narrative_pdf. See schedule_file_path."),
    special_provision_pdf_path: Optional[str] = Form(
        None, description="Storage path in place of special_provision_pdf."),
    key_map_pdf_path: Optional[str] = Form(
        None, description="Storage path in place of key_map_pdf."),
    estimate_pdf_path: Optional[str] = Form(
        None, description="Storage path in place of estimate_pdf."),
    utility_plan_pdf_paths: Optional[str] = Form(
        None, description="JSON array of Storage paths, parallel to utility_plan_pdfs."),
    project_id: Optional[str] = Form(
        None, description="Client-generated id to reuse as the Storage path prefix for the "
                           "*_path fields above. Required when using them; ignored otherwise "
                           "(a fresh one is minted)."),
    checks: Optional[str] = Form(
        None,
        description="Optional JSON array of the checks to run "
                     "({check_key, category, name, instruction, check_type, "
                     "source_files}). "
                     "When omitted, the full built-in catalog runs.",
    ),
    authorization: Optional[str] = Header(default=None),
) -> dict:
    """Run a schedule compliance review against NJDOT requirements.

    Usable signed-out (``authorization`` absent or invalid just means the
    review isn't persisted to Storage — see ``user_id_from_token_optional``).

    Two ways to supply files, chosen per-document by which field is set:

    - Raw upload (``schedule_file``, ``narrative_pdf``, ...): the original
      path. This endpoint reads the bytes and — for signed-in callers —
      uploads them to Storage itself. The only option for a signed-out
      caller, since there's no stable per-user Storage path to write to.
      Still subject to Vercel's 4.5MB request-body limit.
    - Pre-uploaded path (``schedule_file_path``, ``narrative_pdf_path``,
      ...): for a signed-in caller that already uploaded the file straight
      to Storage from the browser (bypassing this function's request body
      entirely). Requires ``project_id`` — the caller-generated id used as
      the Storage path prefix it uploaded under, reused here as the
      ``review_projects.id`` / Neo4j ``projectId`` so the two agree.

    ``schedule_file``/``narrative_pdf`` are required in the raw-upload case;
    ``schedule_file_path``/``narrative_pdf_path`` are required in the
    pre-uploaded case. Mixing the two for the same document is not supported.
    """
    selected_checks = _parse_checks(checks)
    user_id = user_id_from_token_optional(authorization)
    using_stored_paths = bool(schedule_file_path or narrative_pdf_path)

    if project_id:
        existing_owner = _review_progress.get(project_id, {}).get("user_id")
        if not existing_owner:
            try:
                existing_rows = (
                    get_db().table("review_projects").select("user_id")
                    .eq("id", project_id).limit(1).execute().data
                ) or []
                if existing_rows:
                    existing_owner = existing_rows[0].get("user_id")
            except Exception as exc:
                logger.warning("Ownership pre-check for project_id=%s failed: %s", project_id, exc)
        if existing_owner and existing_owner != user_id:
            raise HTTPException(status_code=403, detail="This project_id belongs to another review.")

    if using_stored_paths:
        if not user_id:
            raise HTTPException(status_code=400, detail="*_path fields require a signed-in session.")
        if not project_id:
            raise HTTPException(status_code=400, detail="project_id is required when using *_path fields.")
        if not schedule_file_path or not narrative_pdf_path:
            raise HTTPException(
                status_code=400,
                detail="schedule_file_path and narrative_pdf_path are both required.",
            )
        for _path in (
            schedule_file_path, narrative_pdf_path, special_provision_pdf_path,
            key_map_pdf_path, estimate_pdf_path,
        ):
            if _path:
                _validate_owned_path(_path, user_id)
        try:
            db = get_db()
            bucket = db.storage.from_(_STORAGE_BUCKET)
            schedule_bytes = bucket.download(schedule_file_path)
            narrative_bytes = bucket.download(narrative_pdf_path)
            sp_bytes = bucket.download(special_provision_pdf_path) if special_provision_pdf_path else None
            keymap_bytes = bucket.download(key_map_pdf_path) if key_map_pdf_path else None
            estimate_bytes = bucket.download(estimate_pdf_path) if estimate_pdf_path else None
            utility_plan_paths_list = json.loads(utility_plan_pdf_paths) if utility_plan_pdf_paths else []
            for _p in utility_plan_paths_list:
                if not isinstance(_p, str):
                    raise HTTPException(status_code=400, detail="utility_plan_pdf_paths entries must be strings.")
                _validate_owned_path(_p, user_id)
            utility_plan_bytes_list = [bucket.download(p) for p in utility_plan_paths_list]
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to fetch stored files: {exc}") from exc
        schedule_path, narrative_path = schedule_file_path, narrative_pdf_path
        sp_path, keymap_path, estimate_path = (
            special_provision_pdf_path, key_map_pdf_path, estimate_pdf_path,
        )
    else:
        if not schedule_file or not narrative_pdf:
            raise HTTPException(
                status_code=400,
                detail="schedule_file and narrative_pdf are required "
                       "(either as files, or as schedule_file_path/narrative_pdf_path).",
            )
        project_id = project_id or str(uuid.uuid4())
        if not project_id or "/" in project_id or project_id in (".", ".."):
            raise HTTPException(status_code=400, detail="Invalid project_id.")
        try:
            schedule_bytes = await schedule_file.read()
            narrative_bytes = await narrative_pdf.read()
            sp_bytes = await special_provision_pdf.read() if special_provision_pdf else None
            keymap_bytes = await key_map_pdf.read() if key_map_pdf else None
            estimate_bytes = await estimate_pdf.read() if estimate_pdf else None
            utility_plan_bytes_list = [await f.read() for f in (utility_plan_pdfs or [])]
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to read uploaded files: {exc}") from exc

        schedule_path = narrative_path = sp_path = keymap_path = estimate_path = None
        if user_id:
            try:
                db = get_db()
                bucket = db.storage.from_(_STORAGE_BUCKET)
                base = f"{user_id}/{project_id}"
                schedule_path = f"{base}/schedule.xer"
                _validate_owned_path(schedule_path, user_id)
                bucket.upload(schedule_path, schedule_bytes, {"upsert": "true"})
                narrative_path = f"{base}/narrative.pdf"
                _validate_owned_path(narrative_path, user_id)
                bucket.upload(narrative_path, narrative_bytes, {"upsert": "true"})
                if sp_bytes:
                    sp_path = f"{base}/special_provision.pdf"
                    _validate_owned_path(sp_path, user_id)
                    bucket.upload(sp_path, sp_bytes, {"upsert": "true"})
                if keymap_bytes:
                    keymap_path = f"{base}/key_map.pdf"
                    _validate_owned_path(keymap_path, user_id)
                    bucket.upload(keymap_path, keymap_bytes, {"upsert": "true"})
                if estimate_bytes:
                    estimate_path = f"{base}/estimate.pdf"
                    _validate_owned_path(estimate_path, user_id)
                    bucket.upload(estimate_path, estimate_bytes, {"upsert": "true"})
            except HTTPException:
                raise
            except Exception:
                # Best-effort — "Re-run" just won't be offered for this project;
                # the review itself should still succeed.
                logger.exception("Failed to persist review files to Storage for project_id=%s", project_id)
                schedule_path = narrative_path = sp_path = keymap_path = estimate_path = None

    _review_progress.pop(project_id, None)  # clear any stale entry from a prior run under this id
    _set_review_progress(project_id, status="queued", message="Review queued…", user_id=user_id)
    background_tasks.add_task(
        _run_review_pipeline_background,
        project_id, schedule_bytes, narrative_bytes, sp_bytes, keymap_bytes, estimate_bytes,
        selected_checks, user_id, utility_plan_bytes_list,
        schedule_path, narrative_path, sp_path, keymap_path, estimate_path,
    )
    return {"project_id": project_id, "status": "processing"}


def _run_review_rerun_background(
    project_id: str,
    schedule_bytes: bytes,
    narrative_bytes: bytes,
    sp_bytes: Optional[bytes],
    keymap_bytes: Optional[bytes],
    estimate_bytes: Optional[bytes],
    selected_checks: Optional[List[CheckDef]],
    user_id: Optional[str],
    row: Dict[str, Any],
) -> None:
    """Runs _run_review_pipeline (reseed=False) for the rerun endpoint,
    updates the review_projects row, and stores the outcome in
    _review_progress. See _run_review_pipeline_background's docstring for
    why every exception must be caught here rather than raised."""
    _set_review_progress(
        project_id, status="running",
        message="Re-running compliance review — this can take several minutes…",
    )
    try:
        result = _run_review_pipeline(
            schedule_bytes, narrative_bytes, sp_bytes, keymap_bytes, estimate_bytes,
            selected_checks, project_id, reseed=False, user_id=user_id,
        )
        update_fields: Dict[str, Any] = {
            "review_result": result,
            "project_name": result.get("project_name") or row.get("project_name"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        new_km_extraction = (result.get("key_map") or {}).get("extraction")
        if not row.get("key_map_extraction") and new_km_extraction:
            update_fields["key_map_extraction"] = new_km_extraction
        new_est_extraction = (result.get("estimate") or {}).get("extraction")
        if not row.get("estimate_extraction") and new_est_extraction:
            update_fields["estimate_extraction"] = new_est_extraction
        get_db().table("review_projects").update(update_fields).eq("id", project_id).execute()
        _set_review_progress(project_id, status="ready", message="Review complete.", result=result)
    except HTTPException as exc:
        logger.exception("Background rerun failed for project_id=%s", project_id)
        _set_review_progress(project_id, status="error", message=str(exc.detail))
    except Exception as exc:
        logger.exception("Background rerun failed for project_id=%s", project_id)
        _set_review_progress(project_id, status="error", message="An unexpected error occurred while running the review.")


@router.post(
    "/review/{project_id}/rerun",
    summary="Re-run a past schedule compliance review",
    description=(
        "Re-executes a previously saved review's original files (fetched "
        "from Storage — no re-upload needed), optionally against an edited "
        "checklist. Overwrites the review_projects row's review_result in "
        "place. Requires the caller's Supabase JWT to match the project's "
        "owner."
    ),
)
async def review_rerun_endpoint(
    project_id: str,
    background_tasks: BackgroundTasks,
    authorization: Optional[str] = Header(default=None),
    checks: Optional[str] = Form(
        None,
        description="Optional JSON array of the checks to run, same shape as "
                     "POST /api/review. Omit to re-run the full built-in catalog.",
    ),
) -> dict:
    """Re-run a saved review against its stored files, without re-uploading."""
    user_id = user_id_from_token(authorization)
    selected_checks = _parse_checks(checks)

    db = get_db()
    rows = (
        db.table("review_projects").select("*").eq("id", project_id).limit(1).execute().data
    ) or []
    if not rows:
        raise HTTPException(status_code=404, detail="Review project not found")
    row = rows[0]
    if row.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="This review does not belong to you")

    schedule_path = row.get("schedule_file_path")
    narrative_path = row.get("narrative_pdf_path")
    sp_path = row.get("special_provision_pdf_path")
    keymap_path = row.get("key_map_pdf_path")
    estimate_path = row.get("estimate_pdf_path")
    if not schedule_path or not narrative_path:
        raise HTTPException(
            status_code=400,
            detail="Original files for this review are not available — re-upload once to enable re-run.",
        )
    for _path in (schedule_path, narrative_path, sp_path, keymap_path, estimate_path):
        if _path:
            _validate_owned_path(_path, user_id)

    try:
        bucket = db.storage.from_(_STORAGE_BUCKET)
        schedule_bytes = bucket.download(schedule_path)
        narrative_bytes = bucket.download(narrative_path)
        sp_bytes = bucket.download(sp_path) if sp_path else None
        keymap_bytes = bucket.download(keymap_path) if keymap_path else None
        estimate_bytes = bucket.download(estimate_path) if estimate_path else None
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch stored files: {exc}") from exc

    _review_progress.pop(project_id, None)  # clear the previous run's stale "ready" entry under this same id
    _set_review_progress(project_id, status="queued", message="Rerun queued…", user_id=user_id)
    background_tasks.add_task(
        _run_review_rerun_background,
        project_id, schedule_bytes, narrative_bytes, sp_bytes, keymap_bytes, estimate_bytes,
        selected_checks, user_id, row,
    )
    return {"project_id": project_id, "status": "processing"}


_DOC_TYPE_TO_COLUMN: Dict[str, str] = {
    "narrative":         "narrative_pdf_path",
    "special_provision": "special_provision_pdf_path",
    "key_map":           "key_map_pdf_path",
    "estimate":          "estimate_pdf_path",
}
# utility_plan deliberately excluded -- never uploaded to Storage.


@router.get("/review/{project_id}/pdf/{doc_type}", summary="Serve a stored review-project PDF")
async def review_pdf_endpoint(
    project_id: str,
    doc_type: str,
    authorization: Optional[str] = Header(default=None),
) -> Response:
    column = _DOC_TYPE_TO_COLUMN.get(doc_type)
    if column is None:
        raise HTTPException(status_code=404, detail=f"Unknown document type: {doc_type!r}")

    user_id = user_id_from_token(authorization)
    db = get_db()
    rows = db.table("review_projects").select("*").eq("id", project_id).limit(1).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Review project not found")
    row = rows[0]
    if row.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="This review does not belong to you")

    path = row.get(column)
    if not path:
        raise HTTPException(status_code=404, detail=f"No {doc_type} PDF stored for this review")
    _validate_owned_path(path, user_id)

    try:
        bucket = db.storage.from_(_STORAGE_BUCKET)
        pdf_bytes = bucket.download(path)
    except Exception as exc:
        logger.exception("Failed to fetch stored file path=%s for project_id=%s", path, project_id)
        raise HTTPException(status_code=502, detail="Failed to fetch stored file") from exc

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{doc_type}.pdf"'},
    )
