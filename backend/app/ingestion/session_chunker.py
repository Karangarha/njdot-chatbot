"""Session-scoped document chunkers for the NJDOT review Q&A feature.

Three document types — all deterministic, zero LLM calls:

  chunk_narrative(pages)               Section-heading-based chunks for the
                                        designer narrative (~10 pages).

  chunk_special_provision(pages)       Sliding-window chunks for large SP PDFs
                                        (~200 pages, 600 tok / 100 overlap).

  xer_to_markdown(activities, cals)    Convert XER activity list to NL-rich
                                        Markdown with all activity names visible.
                                        Also used by the review pipeline.

  xer_to_chunks(activities, cals)      Chunk the Markdown output by ## section
                                        for embedding; one chunk per WBS phase.

Gantt / schedule-printout pages are detected and dropped before chunking
so that pdfplumber text-extraction garbage never reaches the embedder.

Chunk output schema
-------------------
Each dict:
  content   str   – text of the chunk
  metadata  dict  – doc_type, page_pdf (int|None), chunk_index (int),
                    section_heading (str, narrative only),
                    section (str, XER section type),
                    phase (str, XER phase chunks only)
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

import tiktoken

_ENCODING_NAME  = "cl100k_base"
_SP_MAX_TOKENS  = 600
_SP_OVERLAP     = 100
_NAR_MAX_TOKENS = 500   # narrative sections are small; high cap avoids unnecessary splits
_NAR_OVERLAP    = 50
_XER_MAX_TOKENS = 800   # phase tables are data-dense; larger window keeps context together
_XER_OVERLAP    = 150


# ── Shared encoder ─────────────────────────────────────────────────────────────

def _get_enc() -> tiktoken.Encoding:
    return tiktoken.get_encoding(_ENCODING_NAME)


# ── Gantt / schedule-printout page detection ───────────────────────────────────

_DATE_RE       = re.compile(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b')
_ACT_CODE_RE   = re.compile(r'^[A-Z]\d{3,5}\b')
_SCHED_HEADERS = {
    'activity id', 'activity name', 'orig dur', 'rem dur',
    'total float', 'early start', 'early finish',
    'late start',  'late finish',  'start', 'finish', 'duration',
}


def _is_gantt_page(text: str) -> bool:
    """Return True when the page looks like a printed schedule / Gantt chart."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False

    # Heuristic 1 — header row contains ≥3 known schedule column keywords
    header_blob = ' '.join(lines[:6]).lower()
    if sum(1 for kw in _SCHED_HEADERS if kw in header_blob) >= 3:
        return True

    # Heuristic 2 — ≥30% of lines contain a date pattern
    if sum(1 for ln in lines if _DATE_RE.search(ln)) / len(lines) >= 0.30:
        return True

    # Heuristic 3 — ≥35% of lines start with an activity code (A1000, M100…)
    if sum(1 for ln in lines if _ACT_CODE_RE.match(ln)) / len(lines) >= 0.35:
        return True

    return False


def _filter_pages(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove blank and Gantt pages from a PDFParser page list."""
    return [
        p for p in pages
        if p.get("text", "").strip() and not _is_gantt_page(p["text"])
    ]


# ── Narrative chunker (section-heading based) ──────────────────────────────────

# Matches lines that are section headings:
#   • Title Case:  "Anticipated Production Rates"
#   • ALL CAPS:    "PROJECT DESCRIPTION"
#   • Optional trailing colon
_HEADING_RE = re.compile(
    r'^(?:'
    r'[A-Z][a-z]+(?:\s+[A-Za-z&()/\-]+){1,7}'   # Title Case (2–8 words)
    r'|[A-Z][A-Z\s&()/\-]{4,60}'                  # ALL CAPS
    r')(?::)?$'
)

# Tokens that should NOT be treated as headings even if they look like one
_HEADING_BLACKLIST = {
    'note', 'notes', 'figure', 'table', 'see', 'ref',
}


def _is_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 80:
        return False
    if line[-1] in '.,;)0123456789':
        return False
    if re.match(r'^\d+[.)]\s', line):          # numbered list item
        return False
    if line.split()[0].lower() in _HEADING_BLACKLIST:
        return False
    return bool(_HEADING_RE.match(line))


def chunk_narrative(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Chunk a designer narrative PDF into section-based chunks.

    Each detected heading opens a new chunk.  Content between headings is
    accumulated and then split with a 500-token ceiling if oversized.

    Parameters
    ----------
    pages : list[dict]
        Output of ``PDFParser.extract_text()``.

    Returns
    -------
    list[dict]
    """
    enc   = _get_enc()
    pages = _filter_pages(pages)

    # ── Walk lines, group by section heading ──────────────────────────────────
    sections: List[Dict[str, Any]] = []
    cur_heading = "Introduction"
    cur_page    = pages[0]["page_num"] if pages else 1
    cur_lines:  List[str] = []

    def _flush() -> None:
        text = "\n".join(cur_lines).strip()
        if text:
            sections.append({
                "heading": cur_heading,
                "page":    cur_page,
                "text":    text,
            })

    for page in pages:
        for raw in page["text"].splitlines():
            line = raw.strip()
            if not line:
                if cur_lines:
                    cur_lines.append("")
                continue
            if _is_heading(line):
                _flush()
                cur_heading = line.rstrip(":")
                cur_page    = page["page_num"]
                cur_lines   = []
            else:
                cur_lines.append(line)

    _flush()

    # ── Convert sections → chunks ─────────────────────────────────────────────
    chunks: List[Dict[str, Any]] = []
    idx = 0

    for sec in sections:
        header = f"{sec['heading']}\n"
        body   = sec["text"]
        full   = (header + body).strip()
        toks   = enc.encode(full)

        if len(toks) <= _NAR_MAX_TOKENS:
            chunks.append({
                "content": full,
                "metadata": {
                    "doc_type":        "designer_narrative",
                    "section_heading": sec["heading"],
                    "page_pdf":        sec["page"],
                    "chunk_index":     idx,
                },
            })
            idx += 1
        else:
            # Sliding window split for any oversized section
            header_toks = enc.encode(header)
            body_toks   = enc.encode(body)
            budget      = _NAR_MAX_TOKENS - len(header_toks)
            start       = 0
            while start < len(body_toks):
                end        = min(start + budget, len(body_toks))
                chunk_text = (header + enc.decode(body_toks[start:end])).strip()
                chunks.append({
                    "content": chunk_text,
                    "metadata": {
                        "doc_type":        "designer_narrative",
                        "section_heading": sec["heading"],
                        "page_pdf":        sec["page"],
                        "chunk_index":     idx,
                    },
                })
                idx += 1
                if end >= len(body_toks):
                    break
                start = end - _NAR_OVERLAP

    return chunks


# ── Special provision chunker (sliding window) ─────────────────────────────────

def _detect_sp_boilerplate(pages: List[Dict[str, Any]], sample_size: int = 15) -> set:
    """
    Find lines that repeat across pages (project title, contract no, page N of M).

    A line is considered boilerplate if it appears on more than half of the
    sampled pages.  Each line is counted at most once per page so frequent
    content lines don't get accidentally stripped.
    """
    from collections import Counter
    sample = pages[:sample_size]
    if not sample:
        return set()

    line_counts: Counter = Counter()
    for page in sample:
        seen: set = set()
        for raw in page["text"].splitlines():
            line = raw.strip()
            if line and line not in seen:
                line_counts[line] += 1
                seen.add(line)

    threshold = max(2, len(sample) * 0.5)
    return {line for line, count in line_counts.items() if count >= threshold}


def chunk_special_provision(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Chunk a Special Provision PDF using a sliding token window.

    Page headers and footers (project name, contract number, page N of M) are
    detected by finding lines that repeat across pages and stripped before
    building the token stream, so they don't pollute every chunk.

    600 tokens max, 100-token overlap.

    Parameters
    ----------
    pages : list[dict]
        Output of ``PDFParser.extract_text()``.

    Returns
    -------
    list[dict]
    """
    enc   = _get_enc()
    pages = _filter_pages(pages)

    boilerplate = _detect_sp_boilerplate(pages)

    # Build a flat token stream, recording which PDF page each token came from
    page_token_starts: List[tuple[int, int]] = []   # (token_offset, page_num)
    all_tokens: List[int] = []

    for page in pages:
        cleaned = "\n".join(
            ln for ln in page["text"].splitlines()
            if ln.strip() not in boilerplate
        ).strip()
        if not cleaned:
            continue
        page_token_starts.append((len(all_tokens), page["page_num"]))
        all_tokens.extend(enc.encode(cleaned + "\n"))

    def _page_at(token_idx: int) -> int:
        page_num = page_token_starts[0][1] if page_token_starts else 1
        for start, pnum in page_token_starts:
            if start <= token_idx:
                page_num = pnum
            else:
                break
        return page_num

    chunks: List[Dict[str, Any]] = []
    start = 0
    idx   = 0

    while start < len(all_tokens):
        end  = min(start + _SP_MAX_TOKENS, len(all_tokens))
        text = enc.decode(all_tokens[start:end]).strip()
        if text:
            chunks.append({
                "content": text,
                "metadata": {
                    "doc_type":    "special_provision",
                    "page_pdf":    _page_at(start),
                    "chunk_index": idx,
                },
            })
            idx += 1
        if end >= len(all_tokens):
            break
        start = end - _SP_OVERLAP

    return chunks


# ── XER → Markdown ─────────────────────────────────────────────────────────────

def _fmt_preds(preds: List[str], limit: int = 4) -> str:
    if not preds:
        return "—"
    shown = ", ".join(preds[:limit])
    return shown + (f" +{len(preds) - limit}" if len(preds) > limit else "")


def xer_to_markdown(
    activities: List[Dict[str, Any]],
    calendars: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Convert parsed XER activities to NL-rich Markdown.

    Produces a document with sections: Calendar, Milestones, Negative Float,
    Mandatory Constraints, and one ## Phase section per WBS path.  Every
    activity name and ID appears verbatim so both vector and keyword search
    can find individual activities.

    Also used by the review pipeline as the schedule representation sent to
    the compliance LLM (instead of raw JSON).

    Parameters
    ----------
    activities : list[dict]
        Output of ``parse_xer_to_json()``.
    calendars : list[dict] | None
        Output of ``parse_xer_calendars()`` (optional).
    """
    lines: List[str] = ["# Project Schedule", ""]

    # ── Calendar ──────────────────────────────────────────────────────────────
    if calendars:
        lines += ["## Calendar", ""]
        for cal in calendars:
            name = cal.get("name") or "Project Calendar"
            work_str = ", ".join(cal.get("work_days", [])) or "unknown"
            lines.append(f"**{name}** — Working days: {work_str}")
            excs = cal.get("exceptions", [])
            if excs:
                exc_str = ", ".join(
                    e["date"] + (f" ({e['name']})" if e.get("name") else "")
                    for e in excs
                )
                lines.append(f"Holiday exceptions: {exc_str}")
        lines.append("")

    # ── Milestones ────────────────────────────────────────────────────────────
    milestones = [a for a in activities if a.get("duration_days", 1) == 0]
    if milestones:
        lines += ["## Milestones", "",
                  "| ID | Name | Date | Float | Predecessors |",
                  "|----|------|------|-------|-------------|"]
        for m in milestones:
            date = m.get("start_date") or m.get("finish_date") or "—"
            lines.append(
                f"| {m['activity_id']} | {m['activity_name']} | {date} | "
                f"{m.get('total_float', 0)} | {_fmt_preds(m.get('predecessors', []))} |"
            )
        lines.append("")

    # ── Negative float ────────────────────────────────────────────────────────
    neg_float = [a for a in activities if (a.get("total_float") or 0) < 0]
    if neg_float:
        lines += ["## Activities with Negative Float", "",
                  "| ID | Name | Start | Finish | Float | Phase |",
                  "|----|------|-------|--------|-------|-------|"]
        for a in sorted(neg_float, key=lambda x: x.get("total_float", 0)):
            phase = " > ".join(a.get("wbs_path") or ["—"])
            lines.append(
                f"| {a['activity_id']} | {a['activity_name']} | "
                f"{a.get('start_date','—')} | {a.get('finish_date','—')} | "
                f"{a.get('total_float','—')} | {phase} |"
            )
        lines.append("")

    # ── Mandatory constraints ─────────────────────────────────────────────────
    constrained = [
        a for a in activities
        if a.get("constraints", {}).get("type") not in (None, "None", "")
    ]
    if constrained:
        lines += ["## Activities with Mandatory Constraints", "",
                  "| ID | Name | Constraint Type | Constraint Date | Phase |",
                  "|----|------|----------------|----------------|-------|"]
        for a in constrained:
            cstr  = a.get("constraints", {})
            phase = " > ".join(a.get("wbs_path") or ["—"])
            lines.append(
                f"| {a['activity_id']} | {a['activity_name']} | "
                f"{cstr.get('type','—')} | {cstr.get('date','—')} | {phase} |"
            )
        lines.append("")

    # ── Per-phase sections ────────────────────────────────────────────────────
    by_phase: Dict[str, List[Dict]] = defaultdict(list)
    for act in activities:
        phase_key = " > ".join(act.get("wbs_path") or ["General"])
        by_phase[phase_key].append(act)

    for phase, acts in sorted(by_phase.items()):
        non_ms = [a for a in acts if a.get("duration_days", 1) != 0]
        if not non_ms:
            continue
        starts   = sorted(a["start_date"]  for a in non_ms if a.get("start_date"))
        finishes = sorted(a["finish_date"] for a in non_ms if a.get("finish_date"))
        date_range = (
            f"{starts[0]} → {finishes[-1]}" if starts and finishes else "dates unknown"
        )
        lines += [
            f"## Phase: {phase}  ({len(non_ms)} activities, {date_range})", "",
            "| ID | Name | Start | Finish | Duration | Float | Predecessors |",
            "|----|------|-------|--------|----------|-------|-------------|",
        ]
        for a in non_ms:
            lines.append(
                f"| {a['activity_id']} | {a['activity_name']} | "
                f"{a.get('start_date','—')} | {a.get('finish_date','—')} | "
                f"{a.get('duration_days','—')} | {a.get('total_float','—')} | "
                f"{_fmt_preds(a.get('predecessors', []))} |"
            )
        lines.append("")

    return "\n".join(lines)


# ── XER → chunks (via Markdown) ────────────────────────────────────────────────

def xer_to_chunks(
    activities: List[Dict[str, Any]],
    calendars: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Convert parsed XER activities into Markdown-based chunks for embedding.

    Calls ``xer_to_markdown()`` and splits the output on ``##`` section
    boundaries.  Each section (Calendar, Milestones, Negative Float, each
    Phase) becomes one chunk.  Sections that exceed ``_XER_MAX_TOKENS`` are
    split with a sliding window that keeps the section header in every piece.

    Parameters
    ----------
    activities : list[dict]
        Output of ``parse_xer_to_json()``.
    calendars : list[dict] | None
        Output of ``parse_xer_calendars()`` (optional; adds a Calendar chunk).
    """
    enc = _get_enc()
    md  = xer_to_markdown(activities, calendars)

    raw_sections = re.split(r"(?=^## )", md, flags=re.MULTILINE)

    chunks: List[Dict[str, Any]] = []
    idx = 0

    for sec in raw_sections:
        sec = sec.strip()
        if not sec or sec.startswith("# "):   # skip the H1 title line
            continue

        first_line = sec.splitlines()[0]
        heading    = first_line.lstrip("#").strip()

        if heading.startswith("Phase:"):
            phase_name = heading.split("Phase:", 1)[1].split("(")[0].strip()
            meta_base: Dict[str, Any] = {
                "doc_type": "xer_activities",
                "section":  "phase",
                "phase":    phase_name,
            }
        else:
            meta_base = {
                "doc_type": "xer_activities",
                "section":  heading.lower().replace(" ", "_"),
            }

        toks = enc.encode(sec)
        if len(toks) <= _XER_MAX_TOKENS:
            chunks.append({"content": sec, "metadata": {**meta_base, "chunk_index": idx}})
            idx += 1
        else:
            header_text = first_line + "\n"
            header_toks = enc.encode(header_text)
            body_toks   = enc.encode(sec[len(header_text):])
            budget      = _XER_MAX_TOKENS - len(header_toks)
            start       = 0
            while start < len(body_toks):
                end        = min(start + budget, len(body_toks))
                chunk_text = (header_text + enc.decode(body_toks[start:end])).strip()
                chunks.append({"content": chunk_text, "metadata": {**meta_base, "chunk_index": idx}})
                idx += 1
                if end >= len(body_toks):
                    break
                start = end - _XER_OVERLAP

    return chunks
