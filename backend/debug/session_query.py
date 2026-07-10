"""Debug script: query a session's RAG chunks with full intermediate output.

Usage:
    python -m debug.session_query SESSION_ID "your question" [--match-count 8] [--show-prompt]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List

import debug  # noqa: F401 — side-effect: adds backend/ to sys.path

from app.config import config
from debug import (
    bold, cyan, dim, green, hr, red, section, subsection, yellow,
)

import openai

from app.database import get_db
from app.generation.llm_client import LLMClient

# ── Same constants and prompts as session.py ──────────────────────────────────
from app.api.session import _QA_SYSTEM, _QA_SYSTEM_GRAPH, _DOC_TAG, _DOC_LABEL
from app.generation.tool_loop import run_tool_loop
from app.graph import load_graph
from app.graph.digest import build_digest
from app.graph.tools import bind_graph_tools


def run_session_query(session_id: str, question: str, match_count: int, show_prompt: bool) -> None:

    db  = get_db()
    oai = openai.OpenAI(api_key=config.OPENAI_API_KEY)

    # ── STAGE 1: Session check ────────────────────────────────────────────────
    section("STAGE 1 — SESSION CHECK")
    try:
        all_rows = (
            db.table("session_chunks")
            .select("doc_type")
            .eq("session_id", session_id)
            .execute()
        )
        total_chunks = len(all_rows.data) if all_rows.data else 0
    except Exception as exc:
        print(red(f"  Error querying session_chunks: {exc}"))
        sys.exit(1)

    if total_chunks == 0:
        print(red(f"  No chunks found for session_id={session_id}"))
        print(yellow("  Run: python -m debug.review --list"))
        sys.exit(1)

    print(f"  session_id:   {bold(session_id)}")
    print(f"  Total chunks: {bold(str(total_chunks))}")

    # Breakdown by doc_type
    counts: Dict[str, int] = {}
    for row in all_rows.data:
        dt = row.get("doc_type", "unknown")
        counts[dt] = counts.get(dt, 0) + 1
    for dt, cnt in sorted(counts.items()):
        print(f"    {dt}: {cnt}")

    # ── STAGE 2: Question embedding ───────────────────────────────────────────
    section("STAGE 2 — QUESTION EMBEDDING")
    print(f"  Question: {bold(question)}")
    print(f"  Model:    {config.EMBEDDING_MODEL}  (dim=1536)")

    t_embed = time.time()
    q_embedding: List[float] = (
        oai.embeddings.create(model=config.EMBEDDING_MODEL, input=[question])
        .data[0]
        .embedding
    )
    print(f"  Elapsed:  {time.time() - t_embed:.2f}s")
    print(f"  Vector shape: [{len(q_embedding)}]")
    preview_vals = ", ".join(f"{v:.5f}" for v in q_embedding[:10])
    print(f"  First 10 values: [{preview_vals}, ...]")

    # ── STAGE 3: Session chunk search ────────────────────────────────────────
    section("STAGE 3 — SESSION CHUNK SEARCH")
    rpc_params = {
        "query_embedding": q_embedding,
        "p_session_id":    session_id,
        "match_count":     match_count,
        "match_threshold": 0.2,
    }
    print(f"  RPC: match_session_chunks")
    print(f"  Params: p_session_id={session_id}  match_count={match_count}  match_threshold=0.2")

    t_srch = time.time()
    session_rows: List[Dict[str, Any]] = (
        db.rpc("match_session_chunks", rpc_params).execute().data
    ) or []
    print(f"  Elapsed: {time.time() - t_srch:.2f}s  |  Results: {len(session_rows)}\n")

    for rank, row in enumerate(session_rows, start=1):
        doc_type   = row.get("doc_type", "")
        similarity = row.get("similarity", 0.0)
        meta       = row.get("metadata", {})
        tag        = _DOC_TAG.get(doc_type, "[Doc]")
        heading    = meta.get("section_heading") or meta.get("phase") or meta.get("activity_id") or "—"
        page       = meta.get("page_pdf")

        print(
            f"{'─' * 80}\n"
            f"  SESSION CHUNK [{rank}]  "
            f"tag={cyan(tag)}  "
            f"doc_type={doc_type}  "
            f"similarity={green(f'{similarity:.4f}')}"
        )
        print(f"    heading / phase / activity_id: {bold(heading)}")
        print(f"    page_pdf: {page if page else '—'}")
        print(f"\n    METADATA (full):")
        for k, v in meta.items():
            print(f"      {dim(k)}: {v}")
        print(f"\n    CONTENT (full):")
        for line in row.get("content", "").splitlines():
            print(f"      {line}")

    # ── STAGE 4: Scheduling manual search ────────────────────────────────────
    section("STAGE 4 — SCHEDULING MANUAL SEARCH (permanent collection)")
    manual_params = {
        "query_embedding":   q_embedding,
        "match_count":       4,
        "filter_collection": "scheduling",
        "match_threshold":   0.3,
    }
    print(f"  RPC: match_chunks")
    print(f"  Params: filter_collection=scheduling  match_count=4  match_threshold=0.3\n")

    t_man = time.time()
    manual_rows: List[Dict[str, Any]] = (
        db.rpc("match_chunks", manual_params).execute().data
    ) or []
    print(f"  Elapsed: {time.time() - t_man:.2f}s  |  Results: {len(manual_rows)}\n")

    for rank, row in enumerate(manual_rows, start=1):
        similarity = row.get("similarity", 0.0)
        meta       = row.get("metadata", {})
        section_id = meta.get("section_id", "—")
        title      = meta.get("section_title", "—")
        page       = meta.get("page_pdf", "—")
        ctx_sum    = meta.get("context_summary", "")

        print(
            f"{'─' * 80}\n"
            f"  MANUAL CHUNK [{rank}]  "
            f"section={cyan(section_id)}  "
            f"similarity={green(f'{similarity:.4f}')}"
        )
        print(f"    section_title: {bold(title)}")
        print(f"    doc:           {meta.get('doc','—')}")
        print(f"    page_pdf:      {page}  /  page_printed: {meta.get('page_printed','—')}")
        if ctx_sum:
            print(f"\n    context_summary:")
            print(f"      {dim(ctx_sum)}")
        print(f"\n    CONTENT (full):")
        for line in row.get("content", "").splitlines():
            print(f"      {line}")

    # ── STAGE 5: Prompt assembly ──────────────────────────────────────────────
    section("STAGE 5 — PROMPT ASSEMBLY")

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
            "similarity": round(row.get("similarity", 0.0), 4),
        })

    for row in manual_rows:
        meta = row.get("metadata", {})
        context_parts.append(
            f"[Manual] {meta.get('section_id','')} {meta.get('section_title','')}\n{row['content']}"
        )
        sources.append({
            "label":      "Construction Scheduling Manual",
            "section_id": meta.get("section_id"),
            "similarity": round(row.get("similarity", 0.0), 4),
        })

    # ── STAGE 5b: Knowledge graph (GraphRAG path) ────────────────────────────
    graph_loaded = load_graph(db, session_id)
    graph_tools = None
    system_prompt = _QA_SYSTEM
    if graph_loaded is not None:
        graph, cpm_summary = graph_loaded
        digest = build_digest(graph, cpm_summary)
        context_parts.insert(0, f"[Graph] Project schedule/narrative digest\n{digest}")
        graph_tools = bind_graph_tools(graph, cpm_summary)
        system_prompt = _QA_SYSTEM_GRAPH
        print(f"  Graph: {green('loaded')} — {graph.number_of_nodes()} nodes, "
              f"{graph.number_of_edges()} edges; digest {len(digest):,} chars; "
              f"{len(graph_tools)} tools bound")
    else:
        print(f"  Graph: {yellow('none')} — plain RAG path")

    context_text = "\n\n---\n\n".join(context_parts)
    user_message = f"Context:\n\n{context_text}\n\nQuestion: {question}"

    print(f"  Context blocks: {len(context_parts)}  ({len(session_rows)} session + {len(manual_rows)} manual)")
    print(f"  Context length: {len(context_text):,} characters")
    print(f"  User message length: {len(user_message):,} characters")

    if show_prompt:
        subsection("SYSTEM PROMPT (full)")
        print(system_prompt)
        subsection("USER MESSAGE (full)")
        print(user_message)

    # ── STAGE 6: LLM call (tool loop when a graph exists) ────────────────────
    section("STAGE 6 — LLM CALL")
    llm = LLMClient()
    print(f"  Provider: {bold(llm.provider)}  Model: {bold(llm.model)}")

    t_llm = time.time()
    print(f"  Calling LLM ... ", end="", flush=True)
    if graph_tools is not None:
        raw_response, tool_trace = run_tool_loop(
            llm, system_prompt, user_message, tools=graph_tools)
    else:
        raw_response = llm.complete(system_prompt, user_message)
        tool_trace = []
    elapsed = time.time() - t_llm
    print(green("✓"))
    print(f"  Elapsed: {elapsed:.1f}s")

    if tool_trace:
        subsection(f"GRAPH TOOL ROUNDS ({len(tool_trace)})")
        for i, t in enumerate(tool_trace, 1):
            print(f"  [{i}] {cyan(t['tool'])}({json.dumps(t['args'])})")
            print(f"      → {dim(t['result_preview'])}")

    # ── STAGE 7: Raw LLM response ─────────────────────────────────────────────
    section("STAGE 7 — RAW LLM RESPONSE")
    print(f"  Length: {len(raw_response):,} characters\n")
    print(raw_response)

    # ── STAGE 8: Final answer + sources ──────────────────────────────────────
    section("STAGE 8 — FINAL ANSWER + SOURCES")

    # Parse answer from response
    answer = raw_response
    try:
        parsed = json.loads(raw_response)
        if isinstance(parsed, dict) and "answer" in parsed:
            answer = parsed["answer"]
    except (json.JSONDecodeError, ValueError):
        pass

    print(f"\n  {bold('ANSWER:')}")
    for line in answer.splitlines():
        print(f"    {line}")

    print(f"\n  {bold('SOURCES')} ({len(sources)}):")
    for i, src in enumerate(sources, start=1):
        label   = src.get("label", "?")
        heading = src.get("heading") or src.get("section_id") or "—"
        page    = src.get("page_pdf") or "—"
        sim     = src.get("similarity", 0.0)
        print(f"    [{i}] {cyan(label)}  |  heading/section: {heading}  |  page_pdf: {page}  |  similarity: {green(f'{sim:.4f}')}")

    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Debug session chunk retrieval + LLM Q&A with full intermediate output.",
    )
    parser.add_argument("session_id",   help="Session UUID from debug.review")
    parser.add_argument("question",     help="Question to ask")
    parser.add_argument("--match-count", type=int, default=8, metavar="N",
                        help="Number of session chunks to retrieve (default: 8)")
    parser.add_argument("--show-prompt", action="store_true",
                        help="Print full system + user prompts")
    args = parser.parse_args()

    run_session_query(
        session_id=args.session_id,
        question=args.question,
        match_count=args.match_count,
        show_prompt=args.show_prompt,
    )


if __name__ == "__main__":
    main()
