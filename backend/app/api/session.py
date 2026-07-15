"""Session-scoped document upload and Q&A endpoints.

Architecture (hybrid): the Special Provision is served by hybrid RAG
(embeddings in Supabase ``session_chunks``); the schedule (XER) and designer
narrative are served by GraphRAG — a CPM-computed knowledge graph in Neo4j
(``project_id`` = ``session_id``), read at query time via an always-injected
digest plus a LangChain tool-calling agent (``create_agent``).

POST /api/session/upload
    Accepts narrative_pdf, special_provision_pdf, and/or xer_file.
    SP is chunked+embedded into Supabase; XER runs through the deterministic
    CPM engine and is seeded into Neo4j; the narrative is sectioned +
    entity-extracted into the same graph. Returns {session_id, status:
    "processing"} immediately.

GET /api/session/status/{session_id}
    SSE stream of ingestion progress events.
    Closes when status reaches "ready" or "error".

POST /api/session/query
    Vector search over the permanent scheduling collection (pre-fetched
    context) plus a tool-calling agent with access to the Neo4j knowledge
    graph and Special Provision retrieval. Returns {answer, sources}.

Required setup — run migrate_session_chunks.sql once (Supabase); Neo4j
Desktop must be running (see app.neo4j_client).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
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
from app.ingestion.embedder          import Embedder
from app.ingestion.pdf_parser        import PDFParser
from app.ingestion.session_chunker   import (
    chunk_narrative,
    chunk_special_provision,
)
from app.neo4j_client import get_neo4j
from app.retrieval_langchain.sp_retriever import build_sp_tool, retrieve_sp_chunks
from app.scheduling import (
    build_calendars,
    build_network,
    cross_check,
    parse_xer_all,
    run_cpm,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/session", tags=["session"])

_DB_BATCH = 50
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


def _extraction_llm() -> Optional[LLMClient]:
    """LLM for narrative entity extraction; None disables extraction gracefully."""
    try:
        return LLMClient()
    except Exception as exc:
        logger.warning("Narrative extraction disabled (no LLM client): %s", exc)
        return None


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
) -> None:
    """
    Hybrid ingestion:
      - Special Provision -> chunk -> embed -> Supabase session_chunks (hybrid RAG path)
      - Schedule (XER) + Designer Narrative -> CPM engine + Neo4j knowledge
        graph (project_id = session_id); Q&A reads them via the graph digest
        and a LangChain tool-calling agent.
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
            _insert_chunks(db, session_id, all_chunks)

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
    "xer_activities":     "Schedule Activities",
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

    if has_graph:
        # ── GraphRAG path: schedule + narrative live in Neo4j, SP via tool ──────
        digest = build_digest(graph, req.session_id)
        graph_tools = build_tools(graph, llm, embeddings.embed_query, project_id=req.session_id)
        sp_tool = build_sp_tool(db, embeddings.embed_query, project_id=req.session_id)
        all_tools = graph_tools + [sp_tool]

        system_prompt = _QA_SYSTEM_GRAPH + f"\n\n[Graph] Project schedule/narrative digest:\n{digest}"
        if context_parts:
            system_prompt += "\n\nPre-fetched context:\n" + "\n\n---\n\n".join(context_parts)

        agent = create_agent(llm, all_tools, system_prompt=system_prompt)
        result = agent.invoke({"messages": [{"role": "user", "content": req.question}]})

        messages = result.get("messages", [])
        answer = messages[-1].content if messages else "Not found in the provided documents."

        sources.insert(0, {"label": "Schedule Graph", "heading": "digest"})
        for m in messages:
            if type(m).__name__ == "ToolMessage":
                sources.append({"label": "Schedule Graph", "tool": getattr(m, "name", "tool")})
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
                "similarity": round(row.get("similarity", 0.0), 3),
            })

        if not context_parts:
            return {"answer": "No relevant content found in the uploaded documents.", "sources": []}

        context_text = "\n\n---\n\n".join(context_parts)
        user_message = f"Context:\n{context_text}\n\nQuestion: {req.question}"
        response = llm.invoke([SystemMessage(content=_QA_SYSTEM), HumanMessage(content=user_message)])
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
