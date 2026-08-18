"""Session-scoped document upload and Q&A endpoints.

Architecture (hybrid): the Special Provision is served by hybrid RAG
(embeddings in Supabase ``session_chunks``); the schedule (XER) and designer
narrative are served by GraphRAG — a CPM-computed knowledge graph in Neo4j
(``project_id`` = ``session_id``), read at query time via an always-injected
digest plus a LangChain tool-calling agent (``create_agent``).

POST /api/session/upload
    Accepts narrative_pdf, special_provision_pdf, xer_file, and/or
    utility_plan_pdfs (test, repeatable — one per utility — see
    ingestion.utility_plan_extractor). SP and each utility plan are
    chunked+embedded into Supabase; XER runs through the deterministic
    CPM engine and is seeded into Neo4j; the narrative is sectioned +
    entity-extracted into the same graph. Returns {session_id,
    status: "processing"} immediately.

GET /api/session/status/{session_id}
    SSE stream of ingestion progress events.
    Closes when status reaches "ready" or "error".

POST /api/session/query
    Vector search over the permanent scheduling collection (pre-fetched
    context) plus a tool-calling agent with access to the Neo4j knowledge
    graph and Special Provision retrieval. Returns {answer, sources}.

Required setup — the ``session_chunks`` table must already exist in
Supabase (no tracked migration defines its DDL; see
``retrieval_langchain.sp_retriever``'s module docstring); Neo4j Desktop
must be running (see app.neo4j_client).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import uuid
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from langchain.agents import create_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import BaseModel

from app.config   import config
from app.database import get_db
from app.generation.llm_client       import LLMClient
from app.graph_neo4j import build_digest, build_tools, clear_project, seed_narrative, seed_schedule
from app.ingestion.chunk_store       import insert_session_chunks
from app.ingestion.embedder          import Embedder
from app.ingestion.pdf_parser        import PDFParser
from app.ingestion.session_chunker   import (
    chunk_narrative,
    chunk_special_provision,
)
from app.neo4j_client import get_neo4j
from app.observability import get_langfuse_handler
from app.ingestion.utility_plan_extractor import extract_utility_plan, render_utility_plan_facts
from app.retrieval_langchain.estimate_retriever import build_estimate_tool
from app.retrieval_langchain.keymap_retriever import build_keymap_tool
from app.retrieval_langchain.sp_retriever import build_sp_tool, retrieve_sp_chunks
from app.retrieval_langchain.utility_plan_retriever import build_utility_plan_tool, retrieve_utility_plan_chunks
from app.compliance.edq import build_edq_coverage_tool
from app.scheduling import (
    build_calendars,
    build_network,
    cross_check,
    parse_xer_all,
    run_cpm,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/session", tags=["session"])

_ANTHROPIC_MODEL = "claude-sonnet-5"

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


def _ingest_utility_plan(session_id: str, utility_plan_bytes: bytes) -> Optional[Dict[str, Any]]:
    """Vision-extract a Utility Agreement Plan sheet and return its
    session_chunks chunk dict (content + metadata, not yet embedded), or
    ``None`` on extraction failure. Shared by both ``_process_session``
    (fresh upload) and ``_process_session_reuse`` (adding one alongside an
    already-seeded review)."""
    primary = ChatOpenAI(model=config.CHAT_MODEL, temperature=0, api_key=config.OPENAI_API_KEY)
    fallback = ChatAnthropic(model=_ANTHROPIC_MODEL, anthropic_api_key=config.ANTHROPIC_API_KEY)
    extraction = extract_utility_plan(utility_plan_bytes, primary.with_fallbacks([fallback]),
                                       project_id=session_id)
    if extraction is None:
        logger.warning("Session %s: utility plan extraction failed", session_id)
        return None
    logger.info("Session %s: utility plan extracted (owner=%s)", session_id, extraction.utility_owner)
    return {
        "content": render_utility_plan_facts(extraction),
        "metadata": {"doc_type": "utility_plan", "utility_owner": extraction.utility_owner},
    }


def _extraction_llm() -> Optional[LLMClient]:
    """LLM for narrative entity extraction; None disables extraction gracefully."""
    try:
        return LLMClient()
    except Exception as exc:
        logger.warning("Narrative extraction disabled (no LLM client): %s", exc)
        return None


def _has_graph(graph: Any, session_id: str) -> bool:
    """Whether any node was seeded into Neo4j for this session (schedule or
    narrative-only — narrative-only sessions have no Project node)."""
    rows = graph.query(
        "MATCH (n {projectId: $sid}) RETURN n LIMIT 1", params={"sid": session_id},
    )
    return bool(rows)


# ── Background ingestion ───────────────────────────────────────────────────────

def _process_session(
    session_id:      str,
    narrative_bytes: Optional[bytes],
    sp_bytes:        Optional[bytes],
    xer_bytes:       Optional[bytes],
    utility_plan_bytes_list: Optional[List[bytes]] = None,
) -> None:
    """
    Hybrid ingestion:
      - Special Provision -> chunk -> embed -> Supabase session_chunks (hybrid RAG path)
      - Schedule (XER) + Designer Narrative -> CPM engine + Neo4j knowledge
        graph (project_id = session_id); Q&A reads them via the graph digest
        and a LangChain tool-calling agent.
      - Utility Agreement Plan (test) -> vision extraction -> Supabase
        session_chunks, same hybrid RAG path as SP.
    Updates _progress at each stage so the SSE stream stays current.
    """
    try:
        db       = get_db()
        embedder = Embedder()
        graph    = get_neo4j()
        embeddings = OpenAIEmbeddings(model=config.EMBEDDING_MODEL, api_key=config.OPENAI_API_KEY)
        all_chunks: List[Dict[str, Any]] = []
        nar_chunks: List[Dict[str, Any]] = []
        graph_saved = False

        # ── 1. Designer Narrative (graph source; not embedded to Supabase) ────
        if narrative_bytes:
            _set_progress(session_id, status="parsing", message="Parsing designer narrative…")
            nar_chunks = _bytes_to_pdf_chunks(narrative_bytes, chunk_narrative)
            logger.info("Session %s: narrative → %d sections (graph)", session_id, len(nar_chunks))

        # ── 2. XER Schedule → CPM → Neo4j knowledge graph ─────────────────────
        if xer_bytes:
            _set_progress(session_id, status="parsing", message="Processing schedule activities…")
            try:
                xer_text   = xer_bytes.decode("utf-8", errors="ignore")
                parsed     = parse_xer_all(xer_text)
                activities = parsed["activities"]
                calendars  = parsed["calendars"]
                project    = parsed["project"]

                # Deterministic CPM pass + cross-check (non-fatal: the graph
                # then simply lacks computed values)
                cpm = xcheck = None
                try:
                    _set_progress(session_id, status="parsing",
                                  message="Computing critical path…")
                    cpm = run_cpm(build_network(activities),
                                  build_calendars(calendars), project)
                    xcheck = cross_check(activities, cpm)
                except Exception:
                    logger.exception("Session %s: CPM computation failed", session_id)

                _set_progress(session_id, status="parsing",
                              message="Seeding knowledge graph…")
                clear_project(graph, session_id)
                seed_schedule(graph, activities, calendars, cpm, xcheck, project,
                              project_id=session_id)

                nar_vectors: List[List[float]] = []
                if nar_chunks:
                    _set_progress(session_id, status="parsing",
                                  message="Extracting narrative entities…")
                    nar_vectors = embeddings.embed_documents([c["content"] for c in nar_chunks])
                seed_narrative(graph, nar_chunks, nar_vectors, activities,
                               project_id=session_id, llm=_extraction_llm())
                graph_saved = True
                logger.info("Session %s: knowledge graph seeded", session_id)
            except HTTPException as exc:
                # XER parse failure is non-fatal; log and continue
                logger.warning("Session %s: XER parse failed — %s", session_id, exc.detail)
            except Exception:
                logger.exception("Session %s: graph seed failed", session_id)
        elif nar_chunks:
            # Narrative without a schedule still gets a (narrative-only) graph
            try:
                _set_progress(session_id, status="parsing",
                              message="Extracting narrative entities…")
                clear_project(graph, session_id)
                nar_vectors = embeddings.embed_documents([c["content"] for c in nar_chunks])
                seed_narrative(graph, nar_chunks, nar_vectors, [],
                               project_id=session_id, llm=_extraction_llm())
                graph_saved = True
            except Exception:
                logger.exception("Session %s: narrative graph seed failed", session_id)

        # ── 3. Special Provision (hybrid RAG path; embedded to Supabase) ───────
        if sp_bytes:
            _set_progress(session_id, status="parsing", message="Parsing special provision PDF…")
            sp_chunks = _bytes_to_sp_chunks(sp_bytes)
            all_chunks.extend(sp_chunks)
            logger.info("Session %s: SP → %d chunks", session_id, len(sp_chunks))

        # ── 3b. Utility Agreement Plans (test; hybrid RAG path; one per utility) ─
        if utility_plan_bytes_list:
            _set_progress(session_id, status="parsing",
                          message=f"Reading {len(utility_plan_bytes_list)} utility agreement plan(s)…")
            for b in utility_plan_bytes_list:
                chunk = _ingest_utility_plan(session_id, b)
                if chunk:
                    all_chunks.append(chunk)

        if not all_chunks and not graph_saved:
            _set_progress(session_id, status="error", message="No content extracted from uploaded files.")
            return

        # ── 4. Parallel embedding + store (SP chunks only) ─────────────────────
        if all_chunks:
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

            _set_progress(session_id, status="storing", message="Storing chunks…")
            insert_session_chunks(db, session_id, all_chunks)

        _set_progress(
            session_id,
            status="ready",
            message="Documents ready. You can now ask questions.",
            chunk_count=len(all_chunks),
            has_graph=graph_saved,
        )
        logger.info("Session %s complete — %d chunks stored, graph=%s",
                    session_id, len(all_chunks), graph_saved)

    except Exception as exc:
        logger.exception("Session %s failed", session_id)
        _set_progress(session_id, status="error", message=str(exc))


def _process_session_reuse(session_id: str, utility_plan_bytes_list: Optional[List[bytes]] = None) -> None:
    """Populate a chat session by reusing an already-seeded ``/api/review``
    project's narrative entities instead of re-extracting them.

    ``session_id`` is literally the review's ``project_id`` — same Neo4j
    namespace, so the schedule (``Activity``/``WBS``/``Calendar``/``Project``
    nodes) is already fully seeded and needs no action here. SP/KeyMap/
    Estimate chunks and extraction JSON are already in Supabase from review
    ingestion time (see ``api.review``'s ``_extract_and_store_keymap`` etc.)
    — nothing to copy. This function's remaining jobs are narrative entity
    extraction (review seeds ``NarrativeChunk`` nodes with ``llm=None``,
    skipping entities; chat needs them for its tool-calling agent) and,
    optionally, ingesting a Utility Agreement Plan sheet uploaded alongside
    the reuse request (test — see ``_ingest_utility_plan``).

    Unlike ``_process_session``, this never calls ``clear_project`` — doing
    so would delete everything the review just seeded under this same id.
    """
    try:
        db = get_db()
        graph = get_neo4j()
        graph_saved = False

        _set_progress(session_id, status="parsing", message="Linking to review data…")

        # ── Narrative entity extraction, reconstructed from already-seeded
        # NarrativeChunk nodes (content + embedding already computed by the
        # review pipeline — no re-parsing, no re-embedding) ────────────────
        nar_rows = graph.query(
            "MATCH (c:NarrativeChunk {projectId: $pid}) "
            "RETURN c.id AS id, c.heading AS heading, c.pagePdf AS pagePdf, "
            "       c.text AS text, c.embedding AS embedding ORDER BY c.id",
            params={"pid": session_id},
        )
        if nar_rows:
            _set_progress(session_id, status="parsing", message="Extracting narrative entities…")
            nar_chunks = [
                {
                    "content": row["text"],
                    "metadata": {
                        "chunk_index": int(row["id"].split(":")[1]),
                        "section_heading": row["heading"],
                        "page_pdf": row["pagePdf"],
                    },
                }
                for row in nar_rows
            ]
            nar_vectors = [row["embedding"] for row in nar_rows]
            activities = graph.query(
                "MATCH (a:Activity {projectId: $pid}) "
                "RETURN a.taskId AS activity_id, a.name AS activity_name",
                params={"pid": session_id},
            )
            seed_narrative(graph, nar_chunks, nar_vectors, activities,
                            project_id=session_id, llm=_extraction_llm())
            graph_saved = True

        # ── Utility Agreement Plans (test) — uploaded alongside the reuse
        # request, not part of the original /api/review submission; one per
        # utility (gas, water/sewer, electric, telecom, ...) ────────────────
        if utility_plan_bytes_list:
            _set_progress(session_id, status="parsing",
                          message=f"Reading {len(utility_plan_bytes_list)} utility agreement plan(s)…")
            chunks = [c for b in utility_plan_bytes_list
                      if (c := _ingest_utility_plan(session_id, b)) is not None]
            if chunks:
                embeddings = OpenAIEmbeddings(model=config.EMBEDDING_MODEL, api_key=config.OPENAI_API_KEY)
                vectors = embeddings.embed_documents([c["content"] for c in chunks])
                for c, v in zip(chunks, vectors):
                    c["embedding"] = v
                insert_session_chunks(db, session_id, chunks)

        chunk_count = (
            db.table("session_chunks").select("id", count="exact")
            .eq("session_id", session_id).limit(1).execute()
        ).count or 0
        _set_progress(
            session_id,
            status="ready",
            message="Documents ready. You can now ask questions.",
            chunk_count=chunk_count,
            has_graph=graph_saved or _has_graph(graph, session_id),
        )
        logger.info("Session %s (reused from review): %d chunks already indexed, graph=%s",
                    session_id, chunk_count, graph_saved)

    except Exception as exc:
        logger.exception("Session %s (reuse) failed", session_id)
        _set_progress(session_id, status="error", message=str(exc))


# ── Upload endpoint ────────────────────────────────────────────────────────────

@router.post("/upload", summary="Upload project documents for session Q&A")
async def upload_session(
    background_tasks:      BackgroundTasks,
    narrative_pdf:         Optional[UploadFile] = File(None, description="Designer narrative PDF"),
    special_provision_pdf: Optional[UploadFile] = File(None, description="Special provision PDF (~200 pages)"),
    xer_file:              Optional[UploadFile] = File(None, description="Primavera P6 XER schedule file"),
    utility_plan_pdfs:     Optional[List[UploadFile]] = File(
        None, description="Utility Agreement Plan sheet PDFs (test) — one per utility (gas, water/sewer, electric, telecom, ...)."),
    project_id:            Optional[str] = Form(
        None,
        description="Reuse an already-seeded /api/review project's data instead of "
                     "re-uploading/re-processing files. When set, narrative_pdf/"
                     "special_provision_pdf/xer_file are ignored; utility_plan_pdfs "
                     "(test) is still processed if given.",
    ),
) -> dict:
    """
    Accept project documents and start background ingestion.
    Returns a session_id immediately; poll /status/{session_id} for progress.

    When ``project_id`` is provided, skips file processing for the three
    review-owned fields and reuses that project's already-seeded data instead
    (see ``_process_session_reuse``) — this is the normal path from the
    review flow, which already chunked/embedded/stored everything via
    ``/api/review`` (SP/KeyMap/Estimate chunks + extractions in Supabase,
    schedule/narrative in Neo4j). ``utility_plan_pdfs`` (test) is not part of
    that flow yet, so they're still read and ingested here if provided.
    """
    utility_plan_bytes_list = [await f.read() for f in (utility_plan_pdfs or [])]

    if project_id:
        session_id = project_id
        _set_progress(session_id, status="queued", message="Linking session to review…")
        background_tasks.add_task(_process_session_reuse, session_id, utility_plan_bytes_list)
        return {"session_id": session_id, "status": "processing"}

    if not any([narrative_pdf, special_provision_pdf, xer_file, utility_plan_bytes_list]):
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
        utility_plan_bytes_list,
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
    restart or page reload), we fall back to Supabase (SP chunks) and Neo4j
    (schedule/narrative graph) to check whether content exists — so restored
    sessions transition to "ready" immediately.
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
            has_graph = _has_graph(get_neo4j(), session_id)
            if count > 0 or has_graph:
                _set_progress(
                    session_id,
                    status="ready",
                    message="Documents ready. You can now ask questions.",
                    chunk_count=count,
                    has_graph=has_graph,
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

_DOC_LABEL: Dict[str, str] = {
    "designer_narrative": "Designer Narrative",
    "special_provision":  "Special Provision",
    "key_map":            "Key Map",
    "estimate":           "Estimate",
    "xer_activities":     "Schedule Activities",
    "utility_plan":       "Utility Agreement Plan",
}

# ToolMessage name -> the source label shown in the chat UI's source pills.
# Anything unlisted (the Cypher/graph tools) is the schedule graph.
_TOOL_SOURCE_LABEL: Dict[str, str] = {
    "search_special_provisions": "Special Provision",
    "search_key_map":            "Key Map",
    "search_estimate":           "Estimate",
    "search_utility_plans":      "Utility Agreement Plan",
    "search_narrative":          "Designer Narrative",
    "get_edq_coverage":          "EDQ Coverage",
}

# ToolMessage name -> doc_type for page-linked citations (matches review.py's
# _DOC_TYPE_TO_COLUMN keys). search_utility_plans excluded: no page_pdf ever.
_TOOL_DOC_TYPE: Dict[str, str] = {
    "search_special_provisions": "special_provision",
    "search_key_map":            "key_map",
    "search_estimate":           "estimate",
    "search_narrative":          "narrative",
}

# Cypher RETURN aliases are unreliable (few-shot examples in cypher_examples.py
# use unaliased "a.taskId" style dotted keys); normalize by stripping any
# "x." prefix and lowercasing before matching.
_ACTIVITY_FIELD_ALIASES: Dict[str, str] = {
    "taskid": "taskId", "name": "name",
    "es": "start", "startdate": "start", "computedearlystart": "start", "start": "start",
    "ef": "finish", "finishdate": "finish", "computedearlyfinish": "finish", "finish": "finish",
}

_QA_SYSTEM = """\
You are an assistant helping an NJDOT engineer review a construction project.
Answer the question using ONLY the context provided below.

Sources in context:
  [SP]         – Special Provision (project-specific contract requirements)
  [Manual]     – NJDOT Construction Scheduling Manual (official standards)

Rules:
- Cite the source tag for every fact you state, e.g. "per [SP]".
- Be concise — one or two paragraphs maximum.
- Do not infer or invent beyond what the context states.
- A [Manual] chunk stating a rule or requirement is evidence of the standard,
  not evidence that this project's schedule complies with it. Never cite a
  [Manual] rule as proof of a project-specific fact — only [SP] context
  confirms what is actually true for this project.

If no context is relevant to the question at all, say exactly:
"Not found in the provided documents."
"""


_QA_SYSTEM_GRAPH = """\
You are an assistant helping an NJDOT engineer review a construction project.

You have access to:
  [Manual]  – NJDOT Construction Scheduling Manual (official standards; may
              appear as pre-fetched context below)
  Tools:
    - query_schedule_graph: answer schedule-logic questions (predecessors/
      successors, negative float, mandatory constraints, WBS/phase filtering,
      narrative-entity mentions) via Cypher against the project's knowledge
      graph.
    - get_critical_path: the precomputed critical path chain(s) — always use
      this instead of query_schedule_graph for critical-path questions.
    - search_narrative: semantic search over the designer narrative's full text.
    - search_special_provisions: search the project's Special Provision text.
    - search_key_map: search the project's key map (key sheet) — utility
      owners, project location (latitude/longitude), route/municipality,
      contract numbers, the index of sheets. Cite as [KeyMap].
    - search_estimate: search the project's DBE Goal Memo / Engineer's
      Estimate — total estimated construction cost, DBE/ESBE goal, and
      project identifiers. Cite as [Estimate].

A digest of the project's schedule (data date, computed finish, critical
path, milestones, phases, cross-check findings) is included below — read it
first; call a tool only for detail beyond what the digest already states.

Rules:
- The graph's float, critical-path, and date values were computed
  deterministically by a CPM engine — they are authoritative. NEVER attempt
  your own float or critical-path arithmetic; call a tool instead.
- Cite a source for every fact: "per the schedule graph", "per [SP]", "per
  [Manual]".
- A [Manual] rule is evidence of the standard, not of this project's
  compliance; only the graph or [SP] facts confirm what is true for this project.
- Be concise — one or two paragraphs, or a short list for enumerations.
- Do not infer or invent beyond what the context and tools return. If neither
  the context nor the tools answer the question, say:
  "Not found in the provided documents."
"""


def _build_tool_sources(messages: List[Any]) -> List[Dict[str, Any]]:
    """Turn each ToolMessage in an agent.invoke() result into 0+ source dicts.

    Detection order: (a) chunk/section payloads with page_pdf -> page-linked
    citations; (b) get_critical_path's chains -> flattened activity list;
    (c) query_schedule_graph records that look like activity rows -> same
    activity-list shape; (d) everything else, or any parse/shape mismatch
    -> today's plain label-only entry. Never raises.
    """
    out: List[Dict[str, Any]] = []
    for m in messages:
        if type(m).__name__ != "ToolMessage":
            continue
        out.extend(_tool_message_to_sources(m))
    return out


def _tool_message_to_sources(m: Any) -> List[Dict[str, Any]]:
    tool_name = getattr(m, "name", "tool")
    label = _TOOL_SOURCE_LABEL.get(tool_name, "Schedule Graph")
    fallback = [{"label": label, "tool": tool_name}]

    try:
        payload = json.loads(m.content)
    except (TypeError, ValueError):
        return fallback

    # Wrap shape-detection logic in try/except to guarantee no exceptions propagate.
    # Any unexpected error (malformed payload, type mismatches, etc.) falls back to label-only.
    try:
        doc_type = _TOOL_DOC_TYPE.get(tool_name)
        if doc_type and isinstance(payload, dict):
            items = payload.get("chunks")
            if items is None:
                items = payload.get("sections")  # search_narrative's shape
            page_items = [it for it in (items or []) if isinstance(it, dict) and it.get("page_pdf") is not None]
            if page_items:
                entries = []
                for it in page_items:
                    entry: Dict[str, Any] = {
                        "label": label, "tool": tool_name, "doc_type": doc_type,
                        "page_pdf": it["page_pdf"],
                    }
                    if it.get("similarity") is not None:
                        entry["similarity"] = round(it["similarity"], 3)
                    if it.get("heading") is not None:
                        entry["heading"] = it["heading"]
                    if it.get("section_id") is not None:
                        entry["section_id"] = it["section_id"]
                    entries.append(entry)
                return entries
            return fallback

        if tool_name == "get_critical_path" and isinstance(payload, dict) and payload.get("chains"):
            activities = _flatten_activities(act for chain in payload["chains"] for act in chain)
            return [{"label": label, "tool": tool_name, "activities": activities}] if activities else fallback

        if tool_name == "query_schedule_graph" and isinstance(payload, list):
            rows = [r for r in payload if isinstance(r, dict) and _looks_like_activity_row(r)]
            activities = _flatten_activities(rows)
            return [{"label": label, "tool": tool_name, "activities": activities}] if activities else fallback

        return fallback
    except Exception:
        return fallback


def _normalize_key(k: str) -> str:
    return k.rsplit(".", 1)[-1].lower()


def _looks_like_activity_row(record: Dict[str, Any]) -> bool:
    norm = {_normalize_key(k) for k in record.keys()}
    return "taskid" in norm and "name" in norm


def _flatten_activities(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        entry = {"taskId": None, "name": None, "start": None, "finish": None}
        for k, v in r.items():
            canon = _ACTIVITY_FIELD_ALIASES.get(_normalize_key(k))
            if canon and entry.get(canon) is None:
                entry[canon] = v
        tid = entry["taskId"]
        if tid is None or tid in seen:
            continue
        seen.add(tid)
        out.append(entry)
    return out


class QueryRequest(BaseModel):
    question:    str
    session_id:  str
    match_count: int = 8


@router.post("/query", summary="Ask a question across session documents + scheduling manual")
async def session_query(req: QueryRequest) -> dict:
    """
    Answer a question using the permanent scheduling-manual collection
    (pre-fetched context) plus, when this session has graph/SP content, a
    tool-calling agent with access to the Neo4j knowledge graph and Special
    Provision retrieval. Session must be in "ready" status before querying.
    """
    progress = _progress.get(req.session_id, {})
    if progress.get("status") != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"Session not ready. Current status: {progress.get('status', 'unknown')}",
        )

    db = get_db()
    embeddings = OpenAIEmbeddings(model=config.EMBEDDING_MODEL, api_key=config.OPENAI_API_KEY)
    q_embedding = embeddings.embed_query(req.question)

    # ── Search scheduling manual (permanent collection) — pre-fetched context ──
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

    context_parts: List[str] = []
    sources:       List[Dict[str, Any]] = []
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

    graph = get_neo4j()
    has_graph = _has_graph(graph, req.session_id)

    primary  = ChatOpenAI(model=config.CHAT_MODEL, temperature=0, api_key=config.OPENAI_API_KEY)
    fallback = ChatAnthropic(model=_ANTHROPIC_MODEL, anthropic_api_key=config.ANTHROPIC_API_KEY)
    llm = primary.with_fallbacks([fallback])

    langfuse_handler = get_langfuse_handler()
    invoke_config = {
        "callbacks": [langfuse_handler] if langfuse_handler else [],
        # Verb-first, low-cardinality name (no session_id/question text) so
        # traces group meaningfully in the Langfuse UI instead of each
        # question minting a distinct name — see langfuse/skills'
        # instrumentation best practices on naming conventions.
        "run_name": "chat-agent-response" if has_graph else "chat-sp-response",
        "metadata": {
            "langfuse_session_id": req.session_id,
            "langfuse_tags": ["chat_agent" if has_graph else "chat_sp_only"],
        },
    }

    if has_graph:
        # ── GraphRAG path: schedule + narrative live in Neo4j, SP via tool ──────
        digest = build_digest(graph, req.session_id)
        graph_tools = build_tools(graph, llm, embeddings.embed_query, project_id=req.session_id)
        sp_tool = build_sp_tool(db, embeddings.embed_query, project_id=req.session_id)
        keymap_tool = build_keymap_tool(db, embeddings.embed_query, project_id=req.session_id)
        estimate_tool = build_estimate_tool(db, embeddings.embed_query, project_id=req.session_id)
        utility_plan_tool = build_utility_plan_tool(db, embeddings.embed_query, project_id=req.session_id)
        edq_tool = build_edq_coverage_tool(graph, project_id=req.session_id)
        all_tools = graph_tools + [sp_tool, keymap_tool, estimate_tool, utility_plan_tool, edq_tool]

        system_prompt = _QA_SYSTEM_GRAPH + f"\n\n[Graph] Project schedule/narrative digest:\n{digest}"
        if context_parts:
            system_prompt += "\n\nPre-fetched context:\n" + "\n\n---\n\n".join(context_parts)

        agent = create_agent(llm, all_tools, system_prompt=system_prompt)
        result = agent.invoke({"messages": [{"role": "user", "content": req.question}]}, config=invoke_config)

        messages = result.get("messages", [])
        answer = messages[-1].content if messages else "Not found in the provided documents."

        sources.insert(0, {"label": "Schedule Graph", "heading": "digest"})
        sources.extend(_build_tool_sources(messages))
    else:
        # ── SP-only path: no graph for this session ─────────────────────────────
        sp_rows = retrieve_sp_chunks(db, embeddings.embed_query, req.session_id, req.question,
                                      match_count=req.match_count)
        for row in sp_rows:
            meta = row.get("metadata", {})
            page = f"p.{meta['page_pdf']}" if meta.get("page_pdf") else ""
            context_parts.append(f"[SP] {page}\n{row['content']}")
            sources.append({
                "label":      _DOC_LABEL.get("special_provision", "Special Provision"),
                "page_pdf":   meta.get("page_pdf"),
                "doc_type":   "special_provision",
                "similarity": round(row.get("similarity", 0.0), 3),
            })

        # Utility plan chunks (test) — same no-graph fallback as SP.
        util_rows = retrieve_utility_plan_chunks(db, embeddings.embed_query, req.session_id, req.question,
                                                  match_count=req.match_count)
        for row in util_rows:
            meta = row.get("metadata", {})
            owner = meta.get("utility_owner") or ""
            context_parts.append(f"[UtilityPlan] {owner}\n{row['content']}")
            sources.append({
                "label":      _DOC_LABEL.get("utility_plan", "Utility Agreement Plan"),
                "similarity": round(row.get("similarity", 0.0), 3),
            })

        if not context_parts:
            return {"answer": "No relevant content found in the uploaded documents.", "sources": []}

        context_text = "\n\n---\n\n".join(context_parts)
        user_message = f"Context:\n{context_text}\n\nQuestion: {req.question}"
        response = llm.invoke(
            [SystemMessage(content=_QA_SYSTEM), HumanMessage(content=user_message)],
            config=invoke_config,
        )
        answer = response.content

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
