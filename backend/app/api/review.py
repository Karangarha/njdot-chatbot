"""POST /api/review — Schedule compliance review endpoint.

Accepts a CPM schedule XER file, a narrative PDF, and an optional Special
Provision PDF. Seeds the schedule + narrative into Neo4j under a fresh
per-review ``project_id`` (multi-project isolation — see
``graph_neo4j.tools``'s Cypher fencing and ``graph_neo4j.seed``'s
composite-key ``MERGE`` writes) and runs the 56-check catalog
through ``app.compliance.eval_engine.evaluate_checks``: one
Pydantic-structured LLM call per check (GPT-4o primary, Claude fallback via
LangChain's ``.with_fallbacks()``), replacing the old single-mega-prompt +
manual JSON parsing.

The Special Provision (if uploaded) is chunked and embedded in-process only
— no persistence needed for a one-shot review, unlike the session-scoped
Supabase retrieval Phase 6 uses for Document Q&A.

Returns the frontend's pre-existing JSON contract via ``_to_frontend_shape``
so ``DocumentReview.tsx`` needs no changes — only this adapter must track
frontend field-shape changes going forward.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import numpy as np
from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.auth import user_id_from_token, user_id_from_token_optional
from app.compliance.catalog import BUILTIN_CHECKS, CheckDef
from app.compliance.eval_engine import evaluate_checks
from app.config import config
from app.database import get_db
from app.graph_neo4j.seed import seed_narrative, seed_schedule, seed_special_provision
from app.graph_neo4j.tools import search_special_provision
from app.ingestion.pdf_parser import PDFParser
from app.ingestion.session_chunker import chunk_narrative, chunk_special_provision
from app.models import ReviewCheckResult, ReviewResponse
from app.neo4j_client import get_neo4j
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

# EvaluationSchema's Pass/Fail/Missing -> the frontend's existing lowercase enum.
_STATUS_MAP = {"Pass": "pass", "Fail": "fail", "Missing": "warning"}


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


def _cosine(a: List[float], b: List[float]) -> float:
    va, vb = np.array(a), np.array(b)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    return float(np.dot(va, vb) / denom) if denom else 0.0


def _build_sp_search_fn(
    sp_bytes: Optional[bytes], embeddings: OpenAIEmbeddings,
) -> Optional[Callable[[str], str]]:
    """In-process cosine-ranked Special Provision search.

    No persistence needed for a one-shot review call — unlike Phase 6's
    session-scoped Supabase retrieval (``retrieval_langchain.sp_retriever``),
    used for Document Q&A where a session_id already exists.
    """
    if not sp_bytes:
        return None
    pages = _bytes_to_pdf_pages(sp_bytes)
    chunks = chunk_special_provision(pages)
    if not chunks:
        return None
    texts = [c["content"] for c in chunks]
    vectors = embeddings.embed_documents(texts)

    def _search(query: str, top_k: int = 5) -> str:
        q_vec = embeddings.embed_query(query)
        scored = sorted(zip(texts, vectors), key=lambda tv: -_cosine(q_vec, tv[1]))
        top = [t for t, _ in scored[:top_k]]
        return "\n\n---\n\n".join(top) if top else "No matching Special Provision text found."

    return _search


def _build_sp_search_fn_from_graph(
    graph: Any, embeddings: OpenAIEmbeddings, project_id: str,
) -> Optional[Callable[[str], str]]:
    """Special Provision search backed by persisted ``SPChunk`` nodes (see
    ``graph_neo4j.seed.seed_special_provision``) — the ``reseed=False`` fast
    path's equivalent of ``_build_sp_search_fn``, without re-parsing,
    re-chunking, or re-embedding the PDF. Only ``embeddings.embed_query``
    runs per check. Returns ``None`` if this project has no SP chunks
    (matches "no SP uploaded" behavior).
    """
    existing = graph.query(
        "MATCH (s:SPChunk {projectId: $pid}) RETURN count(s) AS c LIMIT 1",
        params={"pid": project_id},
    )
    if not existing or not existing[0]["c"]:
        return None

    def _search(query: str, top_k: int = 5) -> str:
        q_vec = embeddings.embed_query(query)
        result = search_special_provision(graph, q_vec, project_id, top_k)
        chunks = result.get("chunks", [])
        if not chunks:
            return "No matching Special Provision text found."
        return "\n\n---\n\n".join(c["content"] for c in chunks)

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
    source_files}`` (the frontend's effective checklist — built-ins plus any
    user customizations). Returns ``None`` when ``raw`` is absent (caller
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
        "manual_review": sum(1 for r in results if r.category == "Manual Review"),
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
    selected_checks: Optional[List[CheckDef]],
    project_id: str,
    reseed: bool = True,
) -> dict:
    """Parse -> CPM -> seed Neo4j -> evaluate the checklist -> frontend shape.

    Shared by the initial upload endpoint and the rerun endpoint — the only
    difference between them is where the three byte blobs come from (a fresh
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
    embeddings = OpenAIEmbeddings(model=config.EMBEDDING_MODEL, api_key=config.OPENAI_API_KEY)

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
        seed_schedule(graph, activities, calendars, cpm, xcheck, project, project_id=project_id)

        narrative_pages = _bytes_to_pdf_pages(narrative_bytes)
        nar_chunks = chunk_narrative(narrative_pages)
        nar_vectors = embeddings.embed_documents([c["content"] for c in nar_chunks]) if nar_chunks else []
        # llm=None: entity extraction (Commitment/Permit/... nodes) is a Document
        # Q&A enrichment, not needed for the checklist itself — the full narrative
        # text (seeded regardless) is what evaluate_checks reads.
        seed_narrative(graph, nar_chunks, nar_vectors, activities, project_id=project_id, llm=None)

        sp_search_fn = _build_sp_search_fn(sp_bytes, embeddings)
        if sp_bytes:
            # Persist chunks/embeddings so a later re-run can reuse them via
            # search_special_provision instead of recreating them here.
            sp_pages = _bytes_to_pdf_pages(sp_bytes)
            sp_chunks = chunk_special_provision(sp_pages)
            if sp_chunks:
                sp_vectors = embeddings.embed_documents([c["content"] for c in sp_chunks])
                seed_special_provision(graph, sp_chunks, sp_vectors, project_id=project_id)

        project_name = (project or {}).get("project_name") or "Unknown"
        duration_days = 0
        if cpm is not None and cpm.data_date and cpm.project_finish:
            duration_days = (cpm.project_finish - cpm.data_date).days
    else:
        # ── Fast path: project already seeded — reuse existing chunks ───────
        project_name, duration_days = _read_project_summary(graph, project_id)
        sp_search_fn = _build_sp_search_fn_from_graph(graph, embeddings, project_id)

    # ── Evaluate the checklist ───────────────────────────────────────────────────
    primary = ChatOpenAI(model=config.CHAT_MODEL, temperature=0, api_key=config.OPENAI_API_KEY)
    fallback = ChatAnthropic(model=_ANTHROPIC_MODEL, anthropic_api_key=config.ANTHROPIC_API_KEY)
    llm = primary.with_fallbacks([fallback])

    try:
        check_results = evaluate_checks(
            selected_checks or BUILTIN_CHECKS, graph, llm,
            sp_search_fn=sp_search_fn, project_id=project_id,
        )
    except Exception as exc:
        logger.exception("Compliance evaluation failed")
        raise HTTPException(status_code=502, detail=f"Compliance evaluation failed: {exc}") from exc

    response = ReviewResponse(
        project_id=project_id,
        project_name=project_name,
        project_duration_days=duration_days,
        summary=_summarize(check_results),
        checks=check_results,
        manual_review_items=[c.name for c in check_results if c.category == "Manual Review"],
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

    return _to_frontend_shape(response)


@router.post(
    "/review",
    summary="Schedule compliance review",
    description=(
        "Accepts a CPM schedule XER file, a narrative PDF, and an optional "
        "Special Provision PDF. Runs the 56-check NJDOT compliance checklist "
        "against a Neo4j-backed knowledge graph (GPT-4o primary, Claude "
        "fallback) and returns a structured JSON report."
    ),
)
async def review_endpoint(
    schedule_file: UploadFile = File(..., description="CPM schedule XER file"),
    narrative_pdf: UploadFile = File(..., description="Project narrative PDF"),
    special_provision_pdf: Optional[UploadFile] = File(
        None, description="Optional Special Provision PDF"),
    checks: Optional[str] = Form(
        None,
        description="Optional JSON array of the checks to run "
                     "({check_key, category, name, instruction, source_files}). "
                     "When omitted, the full built-in 56-check catalog runs.",
    ),
    authorization: Optional[str] = Header(default=None),
) -> dict:
    """Run a schedule compliance review against NJDOT requirements.

    Usable signed-out (``authorization`` absent or invalid just means the
    review isn't persisted to Storage — see ``user_id_from_token_optional``).
    Generates the ``project_id`` that fences this review's Neo4j data and,
    for signed-in callers, doubles as the Storage path prefix and the
    ``review_projects.id`` the frontend inserts under (backend-led upload —
    the frontend never talks to Storage directly, avoiding a double upload
    of the same file bytes).
    """
    selected_checks = _parse_checks(checks)
    project_id = str(uuid.uuid4())

    try:
        schedule_bytes = await schedule_file.read()
        narrative_bytes = await narrative_pdf.read()
        sp_bytes = await special_provision_pdf.read() if special_provision_pdf else None
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read uploaded files: {exc}") from exc

    user_id = user_id_from_token_optional(authorization)
    schedule_path = narrative_path = sp_path = None
    if user_id:
        try:
            db = get_db()
            bucket = db.storage.from_(_STORAGE_BUCKET)
            base = f"{user_id}/{project_id}"
            schedule_path = f"{base}/schedule.xer"
            bucket.upload(schedule_path, schedule_bytes, {"upsert": "true"})
            narrative_path = f"{base}/narrative.pdf"
            bucket.upload(narrative_path, narrative_bytes, {"upsert": "true"})
            if sp_bytes:
                sp_path = f"{base}/special_provision.pdf"
                bucket.upload(sp_path, sp_bytes, {"upsert": "true"})
        except Exception:
            # Best-effort — "Re-run" just won't be offered for this project;
            # the review itself should still succeed.
            logger.exception("Failed to persist review files to Storage for project_id=%s", project_id)
            schedule_path = narrative_path = sp_path = None

    result = _run_review_pipeline(schedule_bytes, narrative_bytes, sp_bytes, selected_checks, project_id)
    result["schedule_file_path"] = schedule_path
    result["narrative_pdf_path"] = narrative_path
    result["special_provision_pdf_path"] = sp_path
    return result


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
    if not schedule_path or not narrative_path:
        raise HTTPException(
            status_code=400,
            detail="Original files for this review are not available — re-upload once to enable re-run.",
        )

    try:
        bucket = db.storage.from_(_STORAGE_BUCKET)
        schedule_bytes = bucket.download(schedule_path)
        narrative_bytes = bucket.download(narrative_path)
        sp_bytes = bucket.download(sp_path) if sp_path else None
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch stored files: {exc}") from exc

    result = _run_review_pipeline(
        schedule_bytes, narrative_bytes, sp_bytes, selected_checks, project_id, reseed=False,
    )

    db.table("review_projects").update({
        "review_result": result,
        "project_name": result.get("project_name") or row.get("project_name"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", project_id).execute()

    return result
