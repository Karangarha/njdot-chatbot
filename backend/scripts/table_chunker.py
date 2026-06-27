"""Table chunking pipeline for NJDOT 2019 Standard Specifications.

Reads spec_full.txt (pdftotext/pdfplumber full-text extraction of
StandSpecRoadBridge.pdf) and produces table_chunks.jsonl — one JSON
object per table row (or per cell for multi-dimensional tables).

WHY ROW-LEVEL CHUNKS
---------------------
The existing pipeline embeds whole tables as single chunks. A query for
"12.5 MM minimum lift thickness" must match against a chunk containing
the full table (all mix sizes, all footnotes). The embedding averages
over all that noise and the right row is drowned out.

Row-level chunks solve this by making each row a self-contained sentence:
    "In Table 401.03.07-6 (Surface Course Thickness Requirements), for HMA
     mix design size 12.5 MM, the minimum allowable compacted lift thickness
     is 1.25 inches."

That sentence embeds close to the query and is directly answerable by the LLM.

PIPELINE STAGES
---------------
1. Detect all named tables (pattern: "Table XXX.XX.XX-N Title")
2. Extract the raw text body of each table
3. Classify table type (simple, multi_dim, formula, gradation, application_rate)
4. Parse rows and column headers
5. Generate natural-language chunk per row (or per cell for multi-dim)
6. Attach footnotes to every chunk from that table
7. Write to table_chunks.jsonl
8. Print validation report

Usage
-----
    python scripts/table_chunker.py
    python scripts/table_chunker.py --input data/spec_full.txt --output data/table_chunks.jsonl
    python scripts/table_chunker.py --detect-only   # show first 10 detected tables and exit
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Paths ─────────────────────────────────────────────────────────────────────

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_INPUT  = _BACKEND_DIR / "data" / "spec_full.txt"
_DEFAULT_OUTPUT = _BACKEND_DIR / "data" / "table_chunks.jsonl"

# ── Table detection regex ─────────────────────────────────────────────────────
#
# Matches lines like:
#   "Table 401.03.07-6 Surface Course Thickness Requirements"
#   "Table 902.02.03-3 HMA Mixture Requirements for Design"
#   "Table 901.03-1A Gradation Requirements for Coarse Aggregate"
#
_TABLE_HEADING_RE = re.compile(
    r'^\s{0,6}Table\s+'                                            # at start of line only
    r'(\d{2,4}\.\d{2}(?:\.\d{2})?(?:\.\d{2})?(?:-\d+)?[A-Z]?)'  # table id
    r'\s+'
    r'([^\n]{5,120})',                                              # table title
    re.IGNORECASE,
)

# Section heading patterns — used to detect where a table body ends
_SECTION_END_RE = re.compile(
    r'^\s*(?:'
    r'\d{3,4}\.\d{2}(?:\.\d{2})*\s+[A-Z]'   # subsection: 401.03 MATERIALS
    r'|DIVISION\s+\d+'                         # DIVISION 400
    r'|SECTION\s+\d+'                          # SECTION 401
    r')',
)

# Footnote line patterns
_FOOTNOTE_RE = re.compile(r'^\s*(\d+[.)]\s+|[*†‡§]\s*|Note[s]?:)', re.IGNORECASE)

# Formula detection -- require actual variable assignment, not just comparison operators
_FORMULA_RE = re.compile(r'PPA\s*=|PA[123]?\s*=|IRI\s*=\s*[\w(]|t\s*[<>]\s*\d')

# Sieve / gradation keywords — require classic sieve sizes (No.4, 3/8", etc.),
# NOT HMA mix designations (9.5 MM, 12.5 MM) which use plain "MM"
_SIEVE_RE = re.compile(r'\bNo\.\s*\d+|\b\d+/\d+["\x27]\b|\bsieve\b', re.IGNORECASE)

# TOC page range to exclude (pages 30–35 per spec)
_TOC_PAGE_RANGE = range(30, 36)

# Pages to parse from (avoid front matter)
_FRONT_MATTER_PAGES = 34


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class RawTable:
    table_id:   str
    table_name: str
    section_id: str        # derived from table_id (e.g. "401.03.07" from "401.03.07-6")
    division:   str        # first 3 digits of section_id
    page_num:   int
    body_lines: List[str] = field(default_factory=list)
    footnotes:  List[str] = field(default_factory=list)
    table_type: str = "simple"  # simple | multi_dim | formula | gradation | application_rate


# ── Step 1: Parse spec_full.txt ───────────────────────────────────────────────

def load_spec_text(path: Path) -> List[Tuple[int, str]]:
    """
    Load spec_full.txt and return list of (page_num, page_text) tuples.
    The file uses [PAGE N] markers written by the extraction script.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    pages: List[Tuple[int, str]] = []

    for block in re.split(r'\[PAGE\s+(\d+)\]', raw)[1:]:
        # re.split with a capture group interleaves numbers and text
        pass

    # Re-do with finditer for clarity
    page_blocks = list(re.finditer(r'\[PAGE\s+(\d+)\](.*?)(?=\[PAGE\s+\d+\]|\Z)', raw, re.DOTALL))
    for m in page_blocks:
        pnum = int(m.group(1))
        text = m.group(2)
        pages.append((pnum, text))

    return pages


# ── Step 2: Detect tables ─────────────────────────────────────────────────────

def detect_tables(pages: List[Tuple[int, str]]) -> List[RawTable]:
    """
    Scan all pages for named table headings and extract their body lines.

    Exclusions:
    - Pages in _TOC_PAGE_RANGE (table-of-contents pages)
    - Pages before _FRONT_MATTER_PAGES
    - Tables with < 2 body lines (likely false positives)
    """
    tables: List[RawTable] = []
    seen_ids: set = set()

    for page_num, text in pages:
        if page_num in _TOC_PAGE_RANGE or page_num <= _FRONT_MATTER_PAGES:
            continue

        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            m = _TABLE_HEADING_RE.search(line)
            if m:
                table_id   = m.group(1).strip()
                table_name = m.group(2).strip()

                # Track for deduplication — resolved below after body is extracted
                pass

                section_id = _section_from_table_id(table_id)
                division   = table_id.split(".")[0] if "." in table_id else table_id[:3]

                # Collect body lines until a blank cluster or section heading
                body_lines: List[str] = []
                footnotes:  List[str] = []
                j = i + 1
                blank_count = 0

                while j < len(lines):
                    bline = lines[j]
                    stripped = bline.strip()

                    # Stop at new section headings
                    if _SECTION_END_RE.match(bline) and len(body_lines) > 2:
                        break

                    # Stop at another table heading (next table starts)
                    if _TABLE_HEADING_RE.search(bline) and len(body_lines) > 2:
                        break

                    # Track consecutive blanks — 3+ signals end of table
                    if stripped == "":
                        blank_count += 1
                        if blank_count >= 3 and len(body_lines) > 2:
                            break
                    else:
                        blank_count = 0

                    # Footnote lines
                    if _FOOTNOTE_RE.match(bline) and len(body_lines) > 1:
                        footnotes.append(stripped)
                    elif stripped:
                        body_lines.append(stripped)

                    j += 1

                if len(body_lines) >= 2:
                    rt = RawTable(
                        table_id=table_id,
                        table_name=table_name,
                        section_id=section_id,
                        division=division,
                        page_num=page_num,
                        body_lines=body_lines,
                        footnotes=footnotes,
                    )
                    # Dedup: keep the occurrence with more body lines
                    # (prose references to a table ID have almost no body)
                    if table_id in seen_ids:
                        for existing_idx, existing in enumerate(tables):
                            if existing.table_id == table_id:
                                if len(body_lines) > len(existing.body_lines):
                                    tables[existing_idx] = rt
                                break
                    else:
                        seen_ids.add(table_id)
                        tables.append(rt)

                i = j
            else:
                i += 1

    return tables


def _section_from_table_id(table_id: str) -> str:
    """Extract section_id from table_id. '401.03.07-6' -> '401.03.07'"""
    # Remove trailing -N or A/B suffix
    return re.sub(r'[-–]\d+[A-Z]?$', '', table_id)


# ── Step 3: Classify table type ───────────────────────────────────────────────

def classify_table(rt: RawTable) -> str:
    """
    Determine table structural type from body lines.

    Types:
    - gradation: wide sieve tables (many columns, sieve keywords)
    - formula: cells contain equations (PPA =, IRI =, ×, ÷)
    - application_rate: 3-4 columns with temp/rate/season
    - multi_dim: 2+ column axes (header row + row key + multiple value cols)
    - simple: 2-column key→value tables
    """
    all_text = " ".join(rt.body_lines)

    if _SIEVE_RE.search(all_text) and _has_many_columns(rt.body_lines):
        return "gradation"

    if _FORMULA_RE.search(all_text):
        return "formula"

    if re.search(r'\b(gallons?|gal|sq\.?\s*yd|season|temperature)\b', all_text, re.I):
        return "application_rate"

    # Count columns using short data rows only (exclude long prose lines)
    data_rows = [l for l in rt.body_lines[1:] if l.strip() and len(l.split()) <= 12]
    if data_rows:
        # Use the median token count of data rows (not the header which may be long)
        token_counts = sorted(len(l.split()) for l in data_rows)
        median_tokens = token_counts[len(token_counts) // 2]
        # 4+ distinct numeric value columns → multi_dim
        numeric_cols = sum(
            1 for t in data_rows[0].split()
            if re.match(r'^[\d]', t)
        )
        if numeric_cols >= 3 and median_tokens >= 5:
            return "multi_dim"

    return "simple"


def _has_many_columns(lines: List[str]) -> bool:
    """True if most lines appear to have 6+ whitespace-separated tokens."""
    token_counts = [len(l.split()) for l in lines if l.strip()]
    if not token_counts:
        return False
    return sum(1 for c in token_counts if c >= 6) > len(token_counts) * 0.4


def _estimate_col_count(header_line: str, data_line: str) -> int:
    """Rough column count from header and first data row."""
    h_tokens = len(header_line.split())
    d_tokens = len(data_line.split())
    return max(h_tokens, d_tokens) // 2  # conservative: pairs of tokens per col


# ── Step 4 + 5: Parse rows and generate chunks ───────────────────────────────

def generate_chunks(rt: RawTable) -> List[Dict[str, Any]]:
    """
    Dispatch to the right parser based on table type.
    Returns a list of chunk dicts ready to write to JSONL.
    """
    # Special-case handlers for known complex tables
    if rt.table_id == "401.03.07-8":
        return _chunks_iri_target_table(rt)

    rt.table_type = classify_table(rt)

    if rt.table_type == "multi_dim":
        return _chunks_multi_dim(rt)
    elif rt.table_type == "formula":
        return _chunks_formula(rt)
    elif rt.table_type == "gradation":
        return _chunks_gradation(rt)
    elif rt.table_type == "application_rate":
        return _chunks_application_rate(rt)
    else:
        return _chunks_simple(rt)


def _chunks_iri_target_table(rt: RawTable) -> List[Dict[str, Any]]:
    """
    Special-case handler for Table 401.03.07-8 Target IRI.
    Uses pdfplumber to get clean structured rows, then generates one chunk
    per (roadway_type, IRI_range, operation_count) cell.
    """
    rt.table_type = "multi_dim"

    # Known column headers for this table
    col_headers = [
        "New Construction or Reconstruction",
        "One paving operation",
        "Two paving operations",
        "Three paving operations",
        "Four or more paving operations",
    ]
    # Roadway type groups: each set of 6 consecutive rows in pdfplumber output
    roadway_types = [
        "Freeways or Limited Access Highways",
        "Other than Freeways or Limited Access Highways with speed limit > 35 MPH",
        "Other than Freeways or Limited Access Highways with speed limit 35 MPH or less",
    ]
    # Base new-construction IRI for each roadway group
    base_iri = [50, 60, 70]

    try:
        import pdfplumber
        _PDF_PATH = _BACKEND_DIR / "data" / "raw_pdfs" / "StandSpecRoadBridge.pdf"
        if not _PDF_PATH.exists():
            raise FileNotFoundError("PDF not found")

        with pdfplumber.open(_PDF_PATH) as pdf:
            page = pdf.pages[rt.page_num - 1]  # 0-indexed
            tables = page.extract_tables()
            if not tables:
                raise ValueError("No tables on page")
            raw_rows = tables[0]

    except Exception:
        # Fall back to generic multi_dim parser if pdfplumber unavailable
        return _chunks_multi_dim(rt)

    chunks: List[Dict[str, Any]] = []
    rows_per_group = len(raw_rows) // len(roadway_types)

    for group_idx, roadway in enumerate(roadway_types):
        group_rows = raw_rows[group_idx * rows_per_group : (group_idx + 1) * rows_per_group]

        for row_idx, row in enumerate(group_rows):
            if not row:
                continue
            iri_range = str(row[0]).strip() if row[0] else ""
            if not iri_range:
                continue

            # Cells 1..4 → column values (col 0 = IRI range, cols 1-4 = operation counts)
            # New Construction value is the first non-None value in col 1, carried forward
            new_constr = str(base_iri[group_idx])  # default
            if row[1] is not None and str(row[1]).strip():
                new_constr = str(row[1]).strip()

            values = {
                col_headers[0]: new_constr,
            }
            for ci, val in enumerate(row[2:], start=1):
                if val is not None and str(val).strip():
                    values[col_headers[ci]] = str(val).strip()

            row_key = f"{roadway}, current IRI {iri_range} in/mi"

            for col_name, value in values.items():
                if not value or value in ("-", "None", "N/A"):
                    continue
                nl = (
                    f"In Table {rt.table_id} ({rt.table_name}), "
                    f"for {roadway} with current IRI (C) {iri_range} in/mi, "
                    f"the Target IRI for {col_name} is {value} in/mi."
                )
                chunks.append(_make_chunk(
                    rt, group_idx * rows_per_group + row_idx + 1,
                    row_key, col_name, value, nl,
                    [roadway, iri_range, col_name, value],
                ))

    return chunks


def _make_chunk(
    rt: RawTable,
    row_idx: int,
    row_key: str,
    col_key: Optional[str],
    value: str,
    natural_language: str,
    raw_row: List[str],
) -> Dict[str, Any]:
    """Build the standard chunk dict and attach footnotes."""
    footnote_strs = [f"[FOOTNOTE]: {fn}" for fn in rt.footnotes]
    full_text = natural_language
    if footnote_strs:
        full_text += "\n" + "\n".join(footnote_strs)

    col_suffix = f"-col-{col_key[:20].replace(' ','_')}" if col_key else ""
    chunk_id   = f"tbl-{rt.table_id}-row-{row_idx}{col_suffix}"

    return {
        "chunk_id":    chunk_id,
        "chunk_type":  "table_row",
        "table_id":    rt.table_id,
        "table_name":  rt.table_name,
        "section":     rt.section_id,
        "division":    rt.division,
        "page":        rt.page_num,
        "table_type":  rt.table_type,
        "row_key":     row_key,
        "col_key":     col_key,
        "footnotes":   rt.footnotes,
        "text":        full_text,
        "raw_row":     raw_row,
    }


# ── Simple 2-column tables ────────────────────────────────────────────────────

def _chunks_simple(rt: RawTable) -> List[Dict[str, Any]]:
    """
    Parse simple 2-column key→value tables.
    Header row = first line; data rows = remainder.

    Handles both wide-space (2+ spaces) and single-space-separated text,
    using a right-split on the last numeric/unit token as the value boundary.
    """
    if not rt.body_lines:
        return []

    # Filter out long prose lines that leaked into the body
    lines = [l for l in rt.body_lines if l.strip() and len(l.split()) <= 15]
    if len(lines) < 2:
        return []

    header = lines[0]
    col_names = _split_two_columns(header)
    col_a = col_names[0] if col_names else "Key"
    col_b = col_names[1] if len(col_names) > 1 else "Value"

    chunks = []
    for i, line in enumerate(lines[1:], start=1):
        # Try wide-space split first
        parts = _split_two_columns(line)
        if len(parts) < 2:
            # Fallback: right-split on last value token (number + optional unit)
            parts = _rsplit_at_last_value(line)
        if len(parts) < 2:
            continue
        key_val = parts[0].strip()
        value   = parts[1].strip()
        if not key_val or not value:
            continue
        # Skip lines that look like prose (sentence fragments)
        if len(key_val.split()) > 8:
            continue

        nl = (
            f"In Table {rt.table_id} ({rt.table_name}), "
            f"for {col_a} {key_val}, "
            f"the {col_b} is {value}."
        )
        chunks.append(_make_chunk(rt, i, key_val, None, value, nl, parts))

    return chunks


def _rsplit_at_last_value(line: str) -> List[str]:
    """
    Split a line into key | value by finding the last numeric value token(s).
    E.g. '12.5 MM 1.25 inches' -> ['12.5 MM', '1.25 inches']
    E.g. 'CSS-1 70 to 140' -> ['CSS-1', '70 to 140']
    """
    # Match trailing value: number (with optional unit) at end of string
    m = re.search(
        r'^(.+?)\s+'
        r'((?:\d[\d.,/]*(?:\s+to\s+\d[\d.,/]*)?)'  # number or range
        r'(?:\s+(?:inch(?:es)?|mm|%|°[CF]|gal|sq|yd|lb|ton|ft|cy|sy))?\s*)$',
        line.strip(), re.IGNORECASE
    )
    if m:
        return [m.group(1).strip(), m.group(2).strip()]
    return [line.strip()]


def _split_two_columns(line: str) -> List[str]:
    """
    Split a line into two columns by the largest whitespace gap (>=2 spaces).
    Falls back to splitting at the midpoint if no gap found.
    """
    # Find all runs of 2+ spaces
    gaps = [(m.start(), m.end()) for m in re.finditer(r'\s{2,}', line)]
    if not gaps:
        tokens = line.split()
        mid = len(tokens) // 2
        return [" ".join(tokens[:mid]), " ".join(tokens[mid:])]

    # Pick the largest gap
    best = max(gaps, key=lambda g: g[1] - g[0])
    return [line[:best[0]].strip(), line[best[1]:].strip()]


# ── Multi-dimensional tables ──────────────────────────────────────────────────

def _chunks_multi_dim(rt: RawTable) -> List[Dict[str, Any]]:
    """
    Parse tables with a row key, a secondary key (col header), and cell values.
    Generates one chunk per (row, column) cell.

    Strategy:
    - First lines without numeric data = column headers (may span multiple lines)
    - Remaining lines = data rows; tokens before first number = row key
    - Multi-line row labels (e.g. "Freeways or Limited / Access Highways") are
      accumulated and carried forward to subsequent IRI-range rows
    """
    lines = [l for l in rt.body_lines if l.strip()]
    if len(lines) < 3:
        return _chunks_simple(rt)

    # Detect header lines: lines before the first line containing standalone numbers
    _NUM_RE = re.compile(r'(?<!\w)\d+(?:\.\d+)?(?!\w)')
    header_lines = []
    data_start = 0
    for idx, line in enumerate(lines):
        if not _NUM_RE.search(line) or idx == 0:
            header_lines.append(line)
            data_start = idx + 1
        else:
            break

    if not header_lines:
        return _chunks_simple(rt)

    col_headers = _parse_col_headers(header_lines)

    # Parse data rows — handle merged/multi-line row labels
    chunks = []
    current_row_label: List[str] = []  # accumulates label tokens across lines

    for row_idx, line in enumerate(lines[data_start:], start=1):
        tokens = line.split()
        if not tokens:
            continue

        # Detect if line has any numeric values (data row) or is pure label
        numeric_tokens = [t for t in tokens if re.match(r'^[-–]?\d+(?:\.\d+)?$|^0$', t)]
        value_like = numeric_tokens or re.search(r'\d+\s+\d+', line)  # "50 50 50 50"

        if not value_like:
            # This line is a label fragment — accumulate
            current_row_label.extend(tokens)
            continue

        # Separate row key tokens from value tokens
        key_tokens = []
        value_tokens = []
        in_values = False
        for t in tokens:
            if not in_values and re.match(r'^[\d≤≥<>–]', t):
                in_values = True
            if in_values:
                value_tokens.append(t)
            else:
                key_tokens.append(t)

        if not value_tokens:
            current_row_label.extend(key_tokens)
            continue

        # Build row key: combine any accumulated label with this line's key
        full_row_key_parts = current_row_label + key_tokens
        row_key = " ".join(full_row_key_parts)
        # Don't reset current_row_label — merged label persists across sub-rows
        # (e.g. all IRI ranges for "Freeways or Limited Access Highways")
        # Only reset when we get a new label-only line cluster
        if key_tokens:
            # There are key tokens on this line — keep accumulated label but don't clear
            pass

        # Generate one chunk per value column
        for col_idx, value in enumerate(value_tokens):
            # Skip range tokens like "to", "≤", "≥" that are part of row key
            if value in ("to", "≤", "≥", "–", "-", "or", "and"):
                continue
            if not value or value in ("-", "–", "—", "N/A"):
                continue

            col_name = col_headers[col_idx] if col_idx < len(col_headers) else f"Column {col_idx+1}"
            nl = (
                f"In Table {rt.table_id} ({rt.table_name}), "
                f"for {row_key}, "
                f"the {col_name} is {value}."
            )
            chunks.append(_make_chunk(
                rt, row_idx, row_key, col_name, value, nl,
                [row_key] + value_tokens,
            ))

    return chunks if chunks else _chunks_simple(rt)


def _parse_col_headers(header_lines: List[str]) -> List[str]:
    """
    Extract column header names from 1-2 header lines.
    For multi-line headers, concatenate corresponding tokens.
    """
    if len(header_lines) == 1:
        # Split by 2+ spaces
        parts = re.split(r'\s{2,}', header_lines[0].strip())
        return [p.strip() for p in parts if p.strip()]

    # Two header lines — zip tokens together
    h1_parts = re.split(r'\s{2,}', header_lines[0].strip())
    h2_parts = re.split(r'\s{2,}', header_lines[1].strip())

    headers = []
    max_len = max(len(h1_parts), len(h2_parts))
    for i in range(max_len):
        a = h1_parts[i].strip() if i < len(h1_parts) else ""
        b = h2_parts[i].strip() if i < len(h2_parts) else ""
        combined = f"{a} {b}".strip()
        headers.append(combined)
    return headers


# ── Formula / Pay Adjustment tables ──────────────────────────────────────────

def _chunks_formula(rt: RawTable) -> List[Dict[str, Any]]:
    """
    Generate chunks for formula/pay-adjustment tables.
    Also adds a worked-example chunk for each row.

    Handles two formats:
    1. Wide-space separated: "Surface    PD ≤ 10    PPA = 4 − (0.4 × PD)"
    2. Test threshold format: "t ≤ 3.0 0" (condition | result)
    """
    lines = [l for l in rt.body_lines if l.strip()]
    if len(lines) < 2:
        return []

    header = lines[0]
    col_names = re.split(r'\s{2,}', header.strip())
    col_names = [c.strip() for c in col_names if c.strip()]

    chunks = []
    for row_idx, line in enumerate(lines[1:], start=1):
        line_s = line.strip()
        if not line_s:
            continue

        # Try wide-space split first
        parts = [p.strip() for p in re.split(r'\s{2,}', line_s) if p.strip()]

        if len(parts) < 2:
            # Fallback: split on the LAST whitespace-separated token that is
            # a formula/number/adjustment (e.g. "0", "-10(t-3)", "PPA = 4 − ...")
            # Pattern: condition part = everything up to last formula/number token
            m_formula = re.search(
                r'^(.+?)\s+'
                r'((?:PPA|IRI|PA[123]?)\s*=\s*.+|-?\d[\d\(\)\.\-\+\*\/\s]*(?:[tPD][\d\-\+\*\(\)]*)*|-100|0)$',
                line_s
            )
            if m_formula:
                parts = [m_formula.group(1).strip(), m_formula.group(2).strip()]
            else:
                # Last resort: split at last space before a standalone number/formula
                tokens = line_s.split()
                if len(tokens) >= 2:
                    # Check if last token looks like a value
                    last = tokens[-1]
                    if re.match(r'^[-+]?\d[\d.]*(?:\([^)]+\))?$|^PPA|^0$', last):
                        parts = [" ".join(tokens[:-1]), last]

        if len(parts) < 2:
            continue

        formula  = parts[-1]
        row_desc = " | ".join(parts[:-1])

        conditions = []
        for ci, part in enumerate(parts[:-1]):
            col_name = col_names[ci] if ci < len(col_names) else f"Condition {ci+1}"
            conditions.append(f"{col_name}: {part}")

        nl = (
            f"In Table {rt.table_id} ({rt.table_name}), "
            f"when {', '.join(conditions)}, "
            f"the pay adjustment is {formula}."
        )
        chunks.append(_make_chunk(rt, row_idx, row_desc, "Formula/Result", formula, nl, parts))

        # Worked example for PPA formulas
        if "PPA" in formula and "PD" in formula:
            sample_pd = _sample_pd_for_formula(formula)
            if sample_pd is not None:
                try:
                    result = _eval_ppa(formula, sample_pd)
                    example_nl = (
                        f"Example using Table {rt.table_id} ({rt.table_name}): "
                        f"when {', '.join(conditions)} and PD = {sample_pd}, "
                        f"PPA = {result:.1f}%."
                    )
                    example_chunk = _make_chunk(
                        rt, row_idx, row_desc, "Formula Example", example_nl, example_nl, parts
                    )
                    example_chunk["chunk_id"] += "-example"
                    chunks.append(example_chunk)
                except Exception:
                    pass

    return chunks


def _sample_pd_for_formula(formula: str) -> Optional[float]:
    """Pick a sample PD value that falls in the formula's range."""
    # Look for range like "PD <= 10" or "10 < PD <= 30"
    m = re.search(r'(\d+)\s*[<≤]\s*PD', formula)
    if m:
        lo = float(m.group(1))
        m2 = re.search(r'PD\s*[<≤]\s*(\d+)', formula)
        hi = float(m2.group(1)) if m2 else lo + 20
        return (lo + hi) / 2
    m = re.search(r'PD\s*[<≤]\s*(\d+)', formula)
    if m:
        return float(m.group(1)) / 2
    return None


def _eval_ppa(formula: str, pd_val: float) -> float:
    """Evaluate a PPA formula string with a given PD value. Very limited eval."""
    # Replace PD with value and common math notation
    expr = formula.replace("PPA", "").replace("=", "").strip()
    expr = expr.replace("PD", str(pd_val))
    expr = expr.replace("×", "*").replace("÷", "/").replace("−", "-")
    expr = re.sub(r'[^\d\s\+\-\*\/\.\(\)]', '', expr)
    return eval(compile(expr, "<string>", "eval"))  # noqa: S307 — controlled input


# ── Gradation / sieve tables ──────────────────────────────────────────────────

def _chunks_gradation(rt: RawTable) -> List[Dict[str, Any]]:
    """
    Generate chunks for wide gradation tables.
    One chunk per aggregate size row, listing all sieve percentages inline.
    """
    lines = [l for l in rt.body_lines if l.strip()]
    if len(lines) < 2:
        return []

    # Header = first line — sieve sizes
    sieve_sizes = lines[0].split()
    chunks = []

    for row_idx, line in enumerate(lines[1:], start=1):
        tokens = line.split()
        if not tokens:
            continue

        # Row key = aggregate type / size designation (first 1-3 non-numeric tokens)
        key_tokens = []
        val_tokens = []
        for t in tokens:
            if re.match(r'^[\d–-]', t) and key_tokens:
                val_tokens.append(t)
            else:
                key_tokens.append(t)

        row_key = " ".join(key_tokens)

        # Build a natural language sentence listing each sieve
        sieve_strs = []
        for si, val in enumerate(val_tokens):
            sieve = sieve_sizes[si + len(key_tokens)] if (si + len(key_tokens)) < len(sieve_sizes) else f"sieve {si+1}"
            if val not in ("-", "–", "—", ""):
                sieve_strs.append(f"{sieve}: {val}%")

        if not sieve_strs:
            continue

        nl = (
            f"In Table {rt.table_id} ({rt.table_name}), "
            f"for aggregate size {row_key}, "
            f"the gradation requirements are: {', '.join(sieve_strs)}."
        )
        chunks.append(_make_chunk(rt, row_idx, row_key, None, line, nl, tokens))

    return chunks


# ── Application rate tables ───────────────────────────────────────────────────

def _chunks_application_rate(rt: RawTable) -> List[Dict[str, Any]]:
    """
    Parse spray temperature + application rate tables.
    Columns: Material | Spray Temp (°F) | Rate (gal/sy) | Season

    Uses regex pattern matching because pdftotext may use single spaces
    between columns, making split-by-2-spaces unreliable.
    """
    lines = [l for l in rt.body_lines if l.strip()]
    if len(lines) < 2:
        return _chunks_simple(rt)

    # Regex to match: material  temp-range  rate-range  season
    _ROW_RE = re.compile(
        r'^([A-Za-z][A-Za-z0-9\-\,\s/]+?)\s+'        # material name
        r'(\d+(?:\.\d+)?\s+to\s+\d+(?:\.\d+)?)\s+'   # temp range
        r'(\d+(?:\.\d+)?\s+to\s+\d+(?:\.\d+)?)\s+'   # rate range
        r'(.+)$'                                        # season
    )
    # Simpler: material followed by a single temp value
    _ROW_SIMPLE_RE = re.compile(
        r'^([A-Za-z][A-Za-z0-9\-\,\s/]+?)\s+'
        r'(\d+(?:\.\d+)?(?:\s+to\s+\d+(?:\.\d+)?)?)°?\s*'
        r'(\d+(?:\.\d+)?(?:\s+to\s+\d+(?:\.\d+)?)?)?\s*'
        r'(.+)?$'
    )

    chunks = []
    for row_idx, line in enumerate(lines[1:], start=1):
        # Skip long prose lines
        if len(line) > 100 and not re.search(r'\d+\s+to\s+\d+', line):
            continue

        m = _ROW_RE.match(line.strip())
        if m:
            material = m.group(1).strip().rstrip(',')
            temp     = m.group(2).strip()
            rate     = m.group(3).strip()
            season   = m.group(4).strip()

            nl = (
                f"In Table {rt.table_id} ({rt.table_name}), "
                f"for {material}, "
                f"the spraying temperature is {temp}°F, "
                f"the application rate is {rate} gallons per square yard, "
                f"season: {season}."
            )
            chunks.append(_make_chunk(
                rt, row_idx, material, None,
                f"{temp} | {rate} | {season}", nl,
                [material, temp, rate, season],
            ))
        else:
            # Fallback: treat as simple 2-col
            parts = _split_two_columns(line)
            if len(parts) < 2:
                parts = _rsplit_at_last_value(line)
            if len(parts) >= 2:
                material = parts[0].strip()
                value    = parts[1].strip()
                if material and value and len(material.split()) <= 6:
                    nl = (
                        f"In Table {rt.table_id} ({rt.table_name}), "
                        f"for {material}, {value}."
                    )
                    chunks.append(_make_chunk(rt, row_idx, material, None, value, nl, parts))

    return chunks if chunks else _chunks_simple(rt)


# ── Step 7: Write output ──────────────────────────────────────────────────────

def write_jsonl(chunks: List[Dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk, ensure_ascii=False) + "\n")


# ── Step 8: Validation report ─────────────────────────────────────────────────

def print_validation(tables: List[RawTable], chunks: List[Dict[str, Any]]) -> None:
    from collections import Counter
    print("\n" + "=" * 60)
    print("VALIDATION REPORT")
    print("=" * 60)
    print(f"Tables detected:    {len(tables)}  (expected ~245)")
    print(f"Total chunks:       {len(chunks)}")
    print()

    type_counts = Counter(t.table_type for t in tables)
    print("Chunks by table type:")
    type_chunk_counts = Counter(c["table_type"] for c in chunks)
    for t, cnt in type_counts.most_common():
        print(f"  {t:20s}  {cnt} tables  {type_chunk_counts.get(t, 0)} chunks")

    print()

    # Spot-check the two eval-failure tables
    for target_id in ["401.03.07-6", "401.03.07-8", "401.03.06-1", "902.08.03-1"]:
        target_chunks = [c for c in chunks if c["table_id"] == target_id]
        print(f"--- Table {target_id} ({len(target_chunks)} chunks) ---")
        for c in target_chunks[:6]:
            print(f"  [{c['row_key']}]  {c['text'][:120]}")
        if len(target_chunks) > 6:
            print(f"  ... and {len(target_chunks) - 6} more")
        print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="NJDOT table chunker")
    parser.add_argument("--input",       default=str(_DEFAULT_INPUT),  help="Path to spec_full.txt")
    parser.add_argument("--output",      default=str(_DEFAULT_OUTPUT), help="Path for table_chunks.jsonl")
    parser.add_argument("--detect-only", action="store_true",          help="Print first 10 detected tables and exit")
    args = parser.parse_args()

    input_path  = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"FAIL Input file not found: {input_path}")
        sys.exit(1)

    print(f"Loading {input_path} ...")
    pages = load_spec_text(input_path)
    print(f"Loaded {len(pages)} pages.")

    print("Detecting tables...")
    tables = detect_tables(pages)
    print(f"Found {len(tables)} tables.")

    if args.detect_only:
        print("\nFirst 10 detected tables:")
        for t in tables[:10]:
            print(f"  p{t.page_num:3d}  {t.table_id:20s}  {t.table_name}  ({len(t.body_lines)} body lines)")
        return

    print("Classifying and generating chunks...")
    all_chunks: List[Dict[str, Any]] = []
    for rt in tables:
        chunks = generate_chunks(rt)
        all_chunks.extend(chunks)

    print(f"Writing {len(all_chunks)} chunks to {output_path} ...")
    write_jsonl(all_chunks, output_path)

    print_validation(tables, all_chunks)
    print(f"\nDone. Output: {output_path}")


if __name__ == "__main__":
    main()
