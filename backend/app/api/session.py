"""Session-scoped document upload and Q&A endpoints.

POST /api/session/upload
    Accepts narrative_pdf, special_provision_pdf, and/or xer_file.
    Parses, chunks, and embeds in the background — no LLM per chunk.
    Returns {session_id, status: "processing"} immediately.

GET /api/session/status/{session_id}
    SSE stream of ingestion progress events.
    Closes when status reaches "ready" or "error".

POST /api/session/query
    Full-text + vector search across session chunks and the permanent
    scheduling collection, then an LLM call for the final answer.
    Returns {answer, sources}.

Required Supabase setup — run migrate_session_chunks.sql once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import uuid
from typing import Any, Dict, List, Optional

import openai
from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config   import config
from app.database import get_db
from app.generation.llm_client       import LLMClient
from app.ingestion.embedder          import Embedder
from app.ingestion.pdf_parser        import PDFParser
from app.ingestion.session_chunker   import (
    chunk_narrative,
    chunk_special_provision,
    xer_to_chunks,
)
from app.api.review import parse_xer_to_json

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/session", tags=["session"])

_DB_BATCH = 50

# ── In-process progress store ──────────────────────────────────────────────────
# Dict is GIL-protected in CPython; safe for single-process FastAPI deploys.

_progress: Dict[str, Dict[str, Any]] = {}


def _set_progress(session_id: str, **kwargs: Any) -> None:
    _progress[session_id] = {**_progress.get(session_id, {}), **kwargs}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _bytes_to_pdf_chunks(raw: bytes, chunker_fn: Any) -> List[Dict[str, Any]]:
    """Write bytes to a temp file, parse with PDFParser, chunk with chunker_fn."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        pages = PDFParser(tmp_path).extract_text()
        return chunker_fn(pages)
    finally:
        os.unlink(tmp_path)


def _bytes_to_sp_chunks(raw: bytes) -> List[Dict[str, Any]]:
    """Chunk a Special Provision PDF with table extraction.

    Keeps the temp file alive while chunk_special_provision runs so pdfplumber
    can re-open it to extract tables as NL sentences.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        pages = PDFParser(tmp_path).extract_text()
        return chunk_special_provision(pages, pdf_path=tmp_path)
    finally:
        os.unlink(tmp_path)


def _insert_chunks(db: Any, session_id: str, chunks: List[Dict[str, Any]]) -> None:
    rows = [
        {
            "session_id": session_id,
            "doc_type":   c["metadata"]["doc_type"],
            "content":    c["content"],
            "embedding":  c["embedding"],
            "metadata":   c["metadata"],
        }
        for c in chunks
    ]
    for i in range(0, len(rows), _DB_BATCH):
        db.table("session_chunks").insert(rows[i : i + _DB_BATCH]).execute()


# ── Background ingestion ───────────────────────────────────────────────────────

def _process_session(
    session_id:      str,
    narrative_bytes: Optional[bytes],
    sp_bytes:        Optional[bytes],
    xer_bytes:       Optional[bytes],
) -> None:
    """
    Parse → chunk → embed (parallel) → insert for all uploaded docs.
    Updates _progress at each stage so the SSE stream stays current.
    No LLM calls — all chunking is deterministic.
    """
    try:
        db       = get_db()
        embedder = Embedder()
        all_chunks: List[Dict[str, Any]] = []

        # ── 1. Designer Narrative ──────────────────────────────────────────────
        if narrative_bytes:
            _set_progress(session_id, status="parsing", message="Parsing designer narrative…")
            nar_chunks = _bytes_to_pdf_chunks(narrative_bytes, chunk_narrative)
            all_chunks.extend(nar_chunks)
            logger.info("Session %s: narrative → %d chunks", session_id, len(nar_chunks))

        # ── 2. XER Schedule Activities ─────────────────────────────────────────
        if xer_bytes:
            _set_progress(session_id, status="parsing", message="Processing schedule activities…")
            try:
                xer_text   = xer_bytes.decode("utf-8", errors="ignore")
                activities = parse_xer_to_json(xer_text)
                xer_chunks = xer_to_chunks(activities)
                all_chunks.extend(xer_chunks)
                logger.info("Session %s: XER → %d chunks", session_id, len(xer_chunks))
            except HTTPException as exc:
                # XER parse failure is non-fatal; log and continue
                logger.warning("Session %s: XER parse failed — %s", session_id, exc.detail)

        # ── 3. Special Provision ───────────────────────────────────────────────
        if sp_bytes:
            _set_progress(session_id, status="parsing", message="Parsing special provision PDF…")
            sp_chunks = _bytes_to_sp_chunks(sp_bytes)
            all_chunks.extend(sp_chunks)
            logger.info("Session %s: SP → %d chunks", session_id, len(sp_chunks))

        if not all_chunks:
            _set_progress(session_id, status="error", message="No content extracted from uploaded files.")
            return

        # ── 4. Parallel embedding ──────────────────────────────────────────────
        total_batches = (len(all_chunks) + embedder.batch_size - 1) // embedder.batch_size
        _set_progress(
            session_id,
            status="embedding",
            message=f"Embedding batch 0/{total_batches}…",
            step=0,
            total=total_batches,
        )

        def _on_progress(done: int, total: int) -> None:
            _set_progress(
                session_id,
                status="embedding",
                message=f"Embedding batch {done}/{total}…",
                step=done,
                total=total,
            )

        embedder.embed_parallel(all_chunks, max_workers=3, on_progress=_on_progress)

        # ── 5. Store ───────────────────────────────────────────────────────────
        _set_progress(session_id, status="storing", message="Storing chunks…")
        _insert_chunks(db, session_id, all_chunks)

        _set_progress(
            session_id,
            status="ready",
            message="Documents ready. You can now ask questions.",
            chunk_count=len(all_chunks),
        )
        logger.info("Session %s complete — %d chunks stored", session_id, len(all_chunks))

    except Exception as exc:
        logger.exception("Session %s failed", session_id)
        _set_progress(session_id, status="error", message=str(exc))


# ── Upload endpoint ────────────────────────────────────────────────────────────

@router.post("/upload", summary="Upload project documents for session Q&A")
async def upload_session(
    background_tasks:      BackgroundTasks,
    narrative_pdf:         Optional[UploadFile] = File(None, description="Designer narrative PDF"),
    special_provision_pdf: Optional[UploadFile] = File(None, description="Special provision PDF (~200 pages)"),
    xer_file:              Optional[UploadFile] = File(None, description="Primavera P6 XER schedule file"),
) -> dict:
    """
    Accept project documents and start background ingestion.
    Returns a session_id immediately; poll /status/{session_id} for progress.
    """
    if not any([narrative_pdf, special_provision_pdf, xer_file]):
        raise HTTPException(status_code=400, detail="At least one file must be provided.")

    session_id = str(uuid.uuid4())
    _set_progress(session_id, status="queued", message="Queued for processing…")

    nar_bytes = await narrative_pdf.read()         if narrative_pdf         else None
    sp_bytes  = await special_provision_pdf.read() if special_provision_pdf else None
    xer_bytes = await xer_file.read()              if xer_file              else None

    background_tasks.add_task(
        _process_session,
        session_id,
        nar_bytes,
        sp_bytes,
        xer_bytes,
    )

    return {"session_id": session_id, "status": "processing"}


# ── SSE status endpoint ────────────────────────────────────────────────────────

@router.get("/status/{session_id}", summary="Stream ingestion progress via SSE")
async def session_status(session_id: str) -> StreamingResponse:
    """
    Server-Sent Events stream. Each event is a JSON progress object:
      {status, message, step?, total?, chunk_count?}
    Stream closes when status is "ready" or "error".

    If the session_id is not in the in-memory store (e.g. after a server
    restart or page reload), we fall back to the database to check whether
    chunks exist — so restored sessions transition to "ready" immediately.
    """
    if session_id not in _progress:
        try:
            db  = get_db()
            res = db.table("session_chunks") \
                    .select("id", count="exact") \
                    .eq("session_id", session_id) \
                    .gt("expires_at", "now()") \
                    .execute()
            count = res.count or 0
            if count > 0:
                _set_progress(
                    session_id,
                    status="ready",
                    message="Documents ready. You can now ask questions.",
                    chunk_count=count,
                )
            else:
                _set_progress(session_id, status="error", message="Session not found or expired.")
        except Exception as exc:
            logger.warning("DB check for session %s failed: %s", session_id, exc)
            _set_progress(session_id, status="error", message="Session not found.")

    async def _generator():
        while True:
            progress = _progress.get(
                session_id,
                {"status": "unknown", "message": "Session not found."},
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


# ── Query endpoint ─────────────────────────────────────────────────────────────

_DOC_TAG: Dict[str, str] = {
    "designer_narrative": "[Narrative]",
    "special_provision":  "[SP]",
    "xer_activities":     "[Schedule]",
}

_DOC_LABEL: Dict[str, str] = {
    "designer_narrative": "Designer Narrative",
    "special_provision":  "Special Provision",
    "xer_activities":     "Schedule Activities",
}

_QA_SYSTEM = """\
You are an assistant helping an NJDOT engineer review a construction project.
Answer the question using ONLY the context provided below.

Sources in context:
  [Narrative]  – Designer Narrative (project-specific schedule explanation)
  [SP]         – Special Provision (project-specific contract requirements)
  [Schedule]   – CPM schedule milestone and phase data
  [Manual]     – NJDOT Construction Scheduling Manual (official standards)

Rules:
- Cite the source tag for every fact you state, e.g. "per [Narrative]".
- Be concise — one or two paragraphs maximum.
- If the answer is not in the context, say exactly: "Not found in the provided documents."
- Do not infer or invent beyond what the context states.
"""


class QueryRequest(BaseModel):
    question:    str
    session_id:  str
    match_count: int = 8


@router.post("/query", summary="Ask a question across session documents + scheduling manual")
async def session_query(req: QueryRequest) -> dict:
    """
    Vector search session chunks + scheduling manual → LLM answer.
    Session must be in "ready" status before querying.
    """
    progress = _progress.get(req.session_id, {})
    if progress.get("status") != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"Session not ready. Current status: {progress.get('status', 'unknown')}",
        )

    db  = get_db()
    oai = openai.OpenAI(api_key=config.OPENAI_API_KEY)

    # ── Embed question ─────────────────────────────────────────────────────────
    q_embedding: List[float] = (
        oai.embeddings.create(model=config.EMBEDDING_MODEL, input=[req.question])
        .data[0]
        .embedding
    )

    # ── Search session chunks ──────────────────────────────────────────────────
    session_rows = (
        db.rpc(
            "match_session_chunks",
            {
                "query_embedding": q_embedding,
                "p_session_id":    req.session_id,
                "match_count":     req.match_count,
                "match_threshold": 0.2,
            },
        )
        .execute()
        .data
    ) or []

    # ── Search scheduling manual (permanent collection) ────────────────────────
    manual_rows = (
        db.rpc(
            "match_chunks",
            {
                "query_embedding":   q_embedding,
                "match_count":       4,
                "filter_collection": "scheduling",
                "match_threshold":   0.3,
            },
        )
        .execute()
        .data
    ) or []

    # ── Build context + source list ────────────────────────────────────────────
    context_parts: List[str] = []
    sources:       List[Dict[str, Any]] = []

    for row in session_rows:
        doc_type = row.get("doc_type", "")
        tag      = _DOC_TAG.get(doc_type, "[Doc]")
        meta     = row.get("metadata", {})
        heading  = meta.get("section_heading") or meta.get("phase") or meta.get("activity_id") or ""
        page     = f"p.{meta['page_pdf']}" if meta.get("page_pdf") else ""
        ref      = f" {heading} {page}".strip()

        context_parts.append(f"{tag}{ref}\n{row['content']}")
        sources.append({
            "label":      _DOC_LABEL.get(doc_type, doc_type),
            "heading":    heading,
            "page_pdf":   meta.get("page_pdf"),
            "similarity": round(row.get("similarity", 0.0), 3),
        })

    for row in manual_rows:
        meta = row.get("metadata", {})
        context_parts.append(
            f"[Manual] {meta.get('section_id', '')} {meta.get('section_title', '')}\n{row['content']}"
        )
        sources.append({
            "label":      "Construction Scheduling Manual",
            "section_id": meta.get("section_id"),
            "similarity": round(row.get("similarity", 0.0), 3),
        })

    if not context_parts:
        return {
            "answer":  "No relevant content found in the uploaded documents.",
            "sources": [],
        }

    context_text = "\n\n---\n\n".join(context_parts)
    user_message = f"Context:\n{context_text}\n\nQuestion: {req.question}"

    # ── LLM answer ─────────────────────────────────────────────────────────────
    llm    = LLMClient()
    answer = llm.complete(_QA_SYSTEM, user_message)

    # ── Persist messages (non-fatal) ───────────────────────────────────────────
    try:
        db.table("session_messages").insert([
            {"session_id": req.session_id, "role": "user",      "content": req.question, "sources": []},
            {"session_id": req.session_id, "role": "assistant",  "content": answer,       "sources": sources},
        ]).execute()
    except Exception as exc:
        logger.warning("Failed to save session messages for %s: %s", req.session_id, exc)

    return {"answer": answer, "sources": sources}


# ── Message history endpoint ───────────────────────────────────────────────────

@router.get("/messages/{session_id}", summary="Load chat history for a session")
async def get_session_messages(session_id: str) -> dict:
    """
    Return all non-expired messages for the session, ordered by creation time.
    Used by the frontend to restore chat history after a page reload.
    """
    db = get_db()
    rows = (
        db.table("session_messages")
        .select("role, content, sources, created_at")
        .eq("session_id", session_id)
        .gt("expires_at", "now()")
        .order("created_at")
        .execute()
        .data
    ) or []
    return {"messages": rows}
