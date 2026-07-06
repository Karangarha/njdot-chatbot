"""Debug script: NJDOT document review pipeline + session ingestion.

Modes
-----
Run compliance review AND ingest documents into a persistent RAG session:

    python -m debug.review schedule.xer narrative.pdf [--sp special_provision.pdf] [--show-prompt]

List saved sessions:

    python -m debug.review --list

Delete a session:

    python -m debug.review --delete SESSION_ID
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Bootstrap sys.path and env ────────────────────────────────────────────────
import debug  # noqa: F401 — side-effect: adds backend/ to sys.path

from app.config import config  # also triggers .env load
from debug import (
    bold, cyan, dim, green, hr, red, section, status_badge, subsection, yellow,
)

import openai

# ── App imports ───────────────────────────────────────────────────────────────
from app.api.review import (
    _OPENAI_MODEL,
    _ANTHROPIC_MODEL,
    _SYSTEM_PROMPT,
    _USER_TEXT,
    parse_xer_to_json,
    parse_xer_calendars,
)
from app.database import get_db
from app.ingestion.embedder import Embedder
from app.ingestion.pdf_parser import PDFParser
from app.ingestion.session_chunker import (
    chunk_narrative, chunk_special_provision, xer_to_chunks, xer_to_markdown,
)

# ── Session state file ────────────────────────────────────────────────────────
_SESSIONS_FILE = Path(__file__).parent / "sessions.json"

_DB_BATCH = 50


# ── Session state helpers ─────────────────────────────────────────────────────

def _load_sessions() -> List[Dict[str, Any]]:
    if _SESSIONS_FILE.exists():
        try:
            return json.loads(_SESSIONS_FILE.read_text(encoding="utf-8")).get("sessions", [])
        except Exception:
            return []
    return []


def _save_sessions(sessions: List[Dict[str, Any]]) -> None:
    _SESSIONS_FILE.write_text(
        json.dumps({"sessions": sessions}, indent=2, default=str),
        encoding="utf-8",
    )


# ── Review LLM call (with raw-text capture) ───────────────────────────────────

def _call_openai_debug(schedule_md: str, narrative_b64: str) -> tuple[str, dict]:
    """Call GPT-4o and return (raw_text, parsed_dict)."""
    client = openai.OpenAI(api_key=config.OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=_OPENAI_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Here is the schedule data in Markdown format:\n{schedule_md}",
                    },
                    {
                        "type": "file",
                        "file": {
                            "filename": "narrative.pdf",
                            "file_data": f"data:application/pdf;base64,{narrative_b64}",
                        },
                    },
                    {"type": "text", "text": _USER_TEXT},
                ],
            },
        ],
    )
    raw_text = response.choices[0].message.content or ""
    parsed = json.loads(raw_text)
    return raw_text, parsed


def _call_anthropic_debug(schedule_md: str, narrative_b64: str) -> tuple[str, dict]:
    """Call Claude and return (raw_text, parsed_dict)."""
    import anthropic as anthropic_sdk
    client = anthropic_sdk.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=_ANTHROPIC_MODEL,
        max_tokens=8192,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Here is the schedule data in Markdown format:\n{schedule_md}",
                    },
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": narrative_b64,
                        },
                        "title": "Project Narrative",
                    },
                    {"type": "text", "text": _USER_TEXT},
                ],
            }
        ],
    )
    raw_text = message.content[0].text if message.content else ""
    parsed = json.loads(raw_text)
    return raw_text, parsed


# ── Session ingestion helpers ─────────────────────────────────────────────────

def _insert_session_chunks(db: Any, session_id: str, chunks: List[Dict[str, Any]]) -> None:
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
        batch = rows[i : i + _DB_BATCH]
        db.table("session_chunks").insert(batch).execute()
        print(f"    Inserted rows {i + 1}–{i + len(batch)} / {len(rows)}")


# ── Modes ─────────────────────────────────────────────────────────────────────

def run_review(xer_path: Path, narrative_path: Path, show_prompt: bool, sp_path: Optional[Path] = None) -> None:
    """Stage 1-8: full review + session ingestion."""

    # ── STAGE 1: File reading ─────────────────────────────────────────────────
    section("STAGE 1 — FILE READING")
    xer_bytes       = xer_path.read_bytes()
    narrative_bytes = narrative_path.read_bytes()
    sp_bytes        = sp_path.read_bytes() if sp_path else None
    xer_text        = xer_bytes.decode("utf-8", errors="ignore")

    print(f"  XER file:           {xer_path}  ({len(xer_bytes):,} bytes)")
    print(f"  Narrative PDF:      {narrative_path}  ({len(narrative_bytes):,} bytes)")
    if sp_path:
        print(f"  Special Provision:  {sp_path}  ({len(sp_bytes):,} bytes)")
    else:
        print(f"  Special Provision:  {dim('(not provided)')}")
    print(f"  XER text length:    {len(xer_text):,} characters")

    # ── STAGE 2: XER parsing ──────────────────────────────────────────────────
    section("STAGE 2 — XER PARSING")
    activities = parse_xer_to_json(xer_text)
    calendars  = parse_xer_calendars(xer_text)

    milestones   = [a for a in activities if a.get("duration_days", 1) == 0]
    regular      = [a for a in activities if a.get("duration_days", 1) != 0]
    neg_float    = [a for a in activities if (a.get("total_float") or 0) < 0]
    no_dates     = [a for a in activities if not a.get("start_date") or not a.get("finish_date")]
    constrained  = [a for a in activities
                    if a.get("constraints", {}).get("type") not in (None, "None", "")]

    wbs_phases = sorted({
        (a.get("wbs_path") or ["Unknown"])[0]
        for a in activities
    })

    print(f"  Total activities:             {bold(str(len(activities)))}")
    print(f"  Milestones (duration=0):      {len(milestones)}")
    print(f"  Regular activities:           {len(regular)}")
    print(f"  Negative float:               {len(neg_float)}")
    print(f"  Mandatory constraints:        {len(constrained)}")
    print(f"  Missing dates:                {len(no_dates)}")
    print(f"\n  Top-level WBS phases ({len(wbs_phases)}):")
    for p in wbs_phases:
        print(f"    • {p}")

    print(f"\n  Calendars found: {len(calendars)}")
    for cal in calendars:
        work_str = ", ".join(cal.get("work_days", [])) or dim("unknown")
        exc_count = len(cal.get("exceptions", []))
        print(f"    • {bold(cal['name'])}  —  work days: {work_str}  |  holiday exceptions: {exc_count}")
        for exc in cal.get("exceptions", [])[:5]:
            print(f"        {dim(exc['date'])} {exc.get('name','')}")
        if exc_count > 5:
            print(f"        {dim(f'… ({exc_count - 5} more)')}")

    if neg_float:
        subsection("Activities with negative float")
        for a in neg_float:
            print(f"    {yellow(a['activity_id'])}: {a['activity_name']}  float={a['total_float']}")

    subsection("First 5 activities (complete JSON)")
    for a in activities[:5]:
        print(json.dumps(a, indent=4))
        hr()

    subsection("All milestone activities")
    for a in milestones:
        print(json.dumps(a, indent=4))
        hr()

    # ── STAGE 3: Prompt assembly ───────────────────────────────────────────────
    section("STAGE 3 — PROMPT ASSEMBLY")
    schedule_md   = xer_to_markdown(activities, calendars)
    narrative_b64 = base64.standard_b64encode(narrative_bytes).decode("utf-8")

    approx_tokens = len(schedule_md) // 4
    print(f"  schedule_md length:       {len(schedule_md):,} characters (~{approx_tokens:,} tokens)")
    print(f"  narrative_b64 length:     {len(narrative_b64):,} characters")

    if show_prompt:
        subsection("SYSTEM PROMPT (full)")
        print(_SYSTEM_PROMPT)
        subsection("USER MESSAGE — schedule markdown")
        print(f"Here is the schedule data in Markdown format:\n{schedule_md}")
        subsection("USER MESSAGE — user text")
        print(_USER_TEXT)

    # ── STAGE 4: LLM call ─────────────────────────────────────────────────────
    section("STAGE 4 — LLM CALL")
    import time
    t0 = time.time()
    model_used = _OPENAI_MODEL

    print(f"  Calling {bold(_OPENAI_MODEL)} ... ", end="", flush=True)
    try:
        raw_text, result = _call_openai_debug(schedule_md, narrative_b64)
        print(green("✓"))
    except Exception as openai_exc:
        print(red(f"✗ ({type(openai_exc).__name__}: {openai_exc})"))
        print(f"  Falling back to {bold(_ANTHROPIC_MODEL)} ... ", end="", flush=True)
        try:
            raw_text, result = _call_anthropic_debug(schedule_md, narrative_b64)
            model_used = _ANTHROPIC_MODEL
            print(green("✓"))
        except Exception as anthropic_exc:
            print(red(f"✗ ({type(anthropic_exc).__name__}: {anthropic_exc})"))
            sys.exit(1)

    elapsed = time.time() - t0
    print(f"  Model used: {bold(model_used)}")
    print(f"  Elapsed: {elapsed:.1f}s")

    # ── STAGE 5: Raw LLM response ─────────────────────────────────────────────
    section("STAGE 5 — RAW LLM RESPONSE")
    print(f"  Response length: {len(raw_text):,} characters\n")
    print(raw_text)

    # ── STAGE 6: Per-check results ────────────────────────────────────────────
    section("STAGE 6 — PARSED CHECK RESULTS")
    checks = result.get("checks", [])
    total  = len(checks)

    for i, check in enumerate(checks, start=1):
        status = check.get("status", "unknown")
        badge  = status_badge(status)
        print(
            f"\n{'═' * 80}\n"
            f"  CHECK [{i:02d}/{total}]  id: {bold(check.get('id','?'))}  "
            f"category: {dim(check.get('category','?'))}\n"
            f"  NAME:     {check.get('name','?')}\n"
            f"  STATUS:   {badge}"
        )
        hr()
        reasoning = check.get("reasoning", "—")
        finding   = check.get("finding", "—")
        evidence  = check.get("evidence", "—")
        print(f"  REASONING:")
        for line in reasoning.splitlines():
            print(f"    {line}")
        print(f"\n  FINDING:")
        print(f"    {finding}")
        print(f"\n  EVIDENCE:")
        print(f"    {evidence}")

    # ── STAGE 7: Summary ──────────────────────────────────────────────────────
    section("STAGE 7 — SUMMARY")
    summary = result.get("summary", {})
    project_name = result.get("project_name", "Unknown")
    duration     = result.get("project_duration_days", "—")
    print(f"  Project:       {bold(project_name)}  ({duration} days)")
    print(f"  Model used:    {bold(model_used)}")
    print(f"  Passed:        {green(str(summary.get('passed',0)))}")
    print(f"  Warnings:      {yellow(str(summary.get('warnings',0)))}")
    print(f"  Failed:        {red(str(summary.get('failed',0)))}")
    print(f"  Manual review: {str(summary.get('manual_review',0))}")

    # ── STAGE 8: Session ingestion ────────────────────────────────────────────
    section("STAGE 8 — SESSION INGESTION")

    session_id = str(uuid.uuid4())
    bar = "█" * 80
    print(f"\n  {bar}")
    print(f"  SESSION ID:  {bold(cyan(session_id))}")
    print(f"  {bar}\n")
    db = get_db()
    all_chunks: List[Dict[str, Any]] = []

    # 8a — Narrative PDF chunking
    subsection("8a — Narrative PDF chunking")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(narrative_bytes)
        tmp_path = tmp.name
    try:
        pages = PDFParser(tmp_path).extract_text()
    finally:
        os.unlink(tmp_path)

    gantt_count = sum(1 for p in pages if not p.get("text", "").strip())
    text_pages  = [p for p in pages if p.get("text", "").strip()]
    print(f"  PDF pages total: {len(pages)}  |  with text: {len(text_pages)}  |  blank/filtered: {gantt_count}")

    nar_chunks = chunk_narrative(pages)
    print(f"  Narrative chunks produced: {len(nar_chunks)}\n")
    for j, chunk in enumerate(nar_chunks):
        meta = chunk.get("metadata", {})
        print(f"  {cyan(f'[NAR-{j+1}]')}  doc_type={meta.get('doc_type','')}  "
              f"page_pdf={meta.get('page_pdf','—')}  "
              f"heading={bold(meta.get('section_heading','—'))}")
        for line in chunk["content"].splitlines():
            print(f"    {line}")
        hr()
    all_chunks.extend(nar_chunks)

    # 8b — XER chunking (markdown-based, one chunk per WBS section)
    subsection("8b — XER schedule chunking")
    xer_ch = xer_to_chunks(activities, calendars)
    print(f"  XER chunks produced: {len(xer_ch)}  (format: NL-rich Markdown by section)\n")
    for j, chunk in enumerate(xer_ch):
        meta    = chunk.get("metadata", {})
        section_label = meta.get("phase") or meta.get("section") or "—"
        print(f"  {cyan(f'[XER-{j+1}]')}  doc_type={meta.get('doc_type','')}  "
              f"section={meta.get('section','')}  {bold(section_label)}")
        for line in chunk["content"].splitlines():
            print(f"    {line}")
        hr()
    all_chunks.extend(xer_ch)

    # 8c — Special Provision PDF chunking (optional)
    subsection("8c — Special Provision PDF chunking")
    if sp_bytes:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(sp_bytes)
            tmp_path_sp = tmp.name
        try:
            sp_pages = PDFParser(tmp_path_sp).extract_text()
        finally:
            os.unlink(tmp_path_sp)

        sp_gantt = sum(1 for p in sp_pages if not p.get("text", "").strip())
        sp_text_pages = [p for p in sp_pages if p.get("text", "").strip()]
        print(f"  SP PDF pages total: {len(sp_pages)}  |  with text: {len(sp_text_pages)}  |  blank/filtered: {sp_gantt}")

        sp_chunks = chunk_special_provision(sp_pages)
        print(f"  SP chunks produced: {len(sp_chunks)}\n")
        for j, chunk in enumerate(sp_chunks):
            meta = chunk.get("metadata", {})
            print(f"  {cyan(f'[SP-{j+1}]')}  doc_type={meta.get('doc_type','')}  "
                  f"page_pdf={meta.get('page_pdf','—')}  chunk_index={meta.get('chunk_index','—')}")
            for line in chunk["content"].splitlines():
                print(f"    {line}")
            hr()
        all_chunks.extend(sp_chunks)
    else:
        print(f"  {dim('No special provision PDF provided — skipping.')}")

    # 8d — Embedding
    subsection("8d — Embedding")
    print(f"  Total chunks to embed: {len(all_chunks)}")
    print(f"  Embedding model: {config.EMBEDDING_MODEL}  (dim=1536)")

    embedder = Embedder()
    done_batches = [0]
    total_batches = (len(all_chunks) + embedder.batch_size - 1) // embedder.batch_size

    def _progress(done: int, total: int) -> None:
        done_batches[0] = done
        print(f"    Batch {done}/{total} embedded ✓")

    embedder.embed_parallel(all_chunks, max_workers=3, on_progress=_progress)
    print(f"  Embedding complete — {len(all_chunks)} chunks, 1536 dims each")

    # 8e — Supabase insert
    subsection("8e — Supabase insert")
    _insert_session_chunks(db, session_id, all_chunks)

    # Save to sessions.json
    sessions = _load_sessions()
    sessions.append({
        "session_id":    session_id,
        "created_at":    datetime.utcnow().isoformat(),
        "xer_file":      str(xer_path),
        "narrative_file": str(narrative_path),
        "sp_file":        str(sp_path) if sp_path else None,
        "project_name":  project_name,
        "chunk_count":   len(all_chunks),
    })
    _save_sessions(sessions)

    # ── Final banner ─────────────────────────────────────────────────────────
    bar = "█" * 80
    print(f"\n  {bar}")
    print(f"  {green('SESSION CREATED SUCCESSFULLY')}")
    print(f"  SESSION ID:   {bold(cyan(session_id))}")
    print(f"  chunk_count:  {len(all_chunks)}  "
          f"(narrative: {len(nar_chunks)}  xer: {len(xer_ch)}"
          + (f"  sp: {len(sp_chunks)}" if sp_bytes else "") + ")")
    print(f"  Saved to:     {_SESSIONS_FILE}")
    print(f"  {bar}")
    print(f"\n  To query this session:")
    print(f"    {cyan(f'Scripts/python.exe -m debug.session_query {session_id} \"your question\"')}")


def list_sessions() -> None:
    section("SAVED DEBUG SESSIONS")
    sessions = _load_sessions()
    if not sessions:
        print("  No sessions found.")
        return

    db = get_db()
    print(f"  {'SESSION ID':<38}  {'PROJECT':<30}  {'CREATED':<20}  CHUNKS")
    hr()
    for s in sessions:
        sid  = s.get("session_id", "?")
        proj = s.get("project_name", "?")[:30]
        ts   = s.get("created_at", "?")[:19]

        # Verify live chunk count from Supabase
        try:
            res   = db.table("session_chunks").select("id", count="exact").eq("session_id", sid).execute()
            live  = res.count or 0
            count = green(str(live)) if live > 0 else red("0 (expired?)")
        except Exception:
            count = yellow("?")

        print(f"  {sid:<38}  {proj:<30}  {ts:<20}  {count}")
    print()
    print(f"  To query: {cyan('python -m debug.session_query SESSION_ID \"question\"')}")
    print(f"  To delete: {cyan('python -m debug.review --delete SESSION_ID')}")


def delete_session(session_id: str) -> None:
    section(f"DELETE SESSION — {session_id}")
    db = get_db()

    # Count first
    try:
        res   = db.table("session_chunks").select("id", count="exact").eq("session_id", session_id).execute()
        count = res.count or 0
        print(f"  Found {count} chunks in Supabase for this session.")
    except Exception as exc:
        print(f"  {yellow(f'Warning: could not count chunks: {exc}')}")
        count = "?"

    confirm = input(f"  Delete {count} chunks? [y/N] ").strip().lower()
    if confirm != "y":
        print("  Aborted.")
        return

    db.table("session_chunks").delete().eq("session_id", session_id).execute()
    print(f"  {green('Deleted.')}  Chunks removed from Supabase.")

    # Remove from sessions.json
    sessions = _load_sessions()
    before = len(sessions)
    sessions = [s for s in sessions if s.get("session_id") != session_id]
    if len(sessions) < before:
        _save_sessions(sessions)
        print(f"  Removed from {_SESSIONS_FILE}.")
    else:
        print(f"  {yellow('Note: session_id not found in sessions.json (already removed or never saved).')}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Debug the NJDOT document review pipeline + session ingestion.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("xer_file",        nargs="?", help="Primavera P6 .xer schedule file")
    parser.add_argument("narrative_pdf",   nargs="?", help="Designer narrative PDF")
    parser.add_argument("--sp",            metavar="SP_PDF", help="Special provision PDF (optional)")
    parser.add_argument("--show-prompt",   action="store_true", help="Print full system + user prompts")
    parser.add_argument("--list",          action="store_true", help="List saved debug sessions")
    parser.add_argument("--delete",        metavar="SESSION_ID", help="Delete a session and its chunks")

    args = parser.parse_args()

    if args.list:
        list_sessions()
    elif args.delete:
        delete_session(args.delete)
    elif args.xer_file and args.narrative_pdf:
        xer_path       = Path(args.xer_file)
        narrative_path = Path(args.narrative_pdf)
        sp_path        = Path(args.sp) if args.sp else None
        if not xer_path.exists():
            print(red(f"Error: XER file not found: {xer_path}"))
            sys.exit(1)
        if not narrative_path.exists():
            print(red(f"Error: Narrative PDF not found: {narrative_path}"))
            sys.exit(1)
        if sp_path and not sp_path.exists():
            print(red(f"Error: Special provision PDF not found: {sp_path}"))
            sys.exit(1)
        run_review(xer_path, narrative_path, show_prompt=args.show_prompt, sp_path=sp_path)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
