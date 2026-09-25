"""Table extraction from NJDOT PDF pages using pdfplumber.

For each pdfplumber page object passed in, ``TableExtractor`` finds bordered
tables, extracts them as pipe-delimited markdown, detects captions and
footnotes above/below, and classifies the table type.

Table types
-----------
``simple``
    ≤ 8 rows, ≤ 5 columns, no nested/merged header rows.
    E.g. small requirement tables, two-column reference tables.

``lookup``
    ≥ 10 rows, 2–6 columns, one key column → value columns.
    E.g. material grade tables, QPL-style entries.

``wide_sparse``
    ≥ 10 columns with ≥ 40 % of cells empty or dash ("–").
    E.g. HMA gradation tables across multiple aggregate sizes.

``multi_header``
    Two or more header rows where at least one contains empty cells that
    indicate merged/spanning column headers.
    E.g. Table 902.02.03-3 with nested VMA / VFA column headers.

Caption detection
-----------------
The area up to ``caption_scan_pts`` (default 60 pt) above the table top
edge is cropped and scanned for a line matching::

    Table  <id>  <title>

where ``<id>`` is like ``902.02.03-3`` or ``901.10.02-1``.

Footnote detection
------------------
The area up to ``footnote_scan_pts`` (default 80 pt) below the table
bottom edge is collected.  Lines are retained as footnotes when they begin
with a digit + period (``1.``), an asterisk (``*``), or ``Note:``
(case-insensitive).

Detection strategies
--------------------
The extractor first tries pdfplumber's native ``find_tables()`` with
strict line-based settings.  If nothing is found, it retries with relaxed
snap and join tolerances.  Errors on individual tables are swallowed so one
bad table never aborts the whole page.

Usage
-----
    import pdfplumber
    from app.ingestion.table_extractor import TableExtractor

    extractor = TableExtractor()

    with pdfplumber.open("StandSpecRoadBridge.pdf") as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            tables = extractor.extract_tables(page, page_pdf=i)
            for t in tables:
                print(t["table_id"], t["table_type"])
                print(t["markdown"])

Return dict schema
------------------
Each dict in the returned list has:

    table_id    str           – e.g. "902.02.03-3" (from caption) or "p441_t1"
    table_title str           – caption text after the table ID
    table_type  str           – simple | lookup | wide_sparse | multi_header
    markdown    str           – pipe-delimited markdown table
    raw_rows    list[list]    – raw cell values (for row-level chunking)
    footnotes   list[str]     – footnote lines found below the table
    bbox        tuple         – (x0, top, x1, bottom) in page coordinates
    page_pdf    int           – 1-based PDF page number
    page_printed int or None  – printed page number (passed in by caller)
    row_count   int
    col_count   int
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Regex patterns ────────────────────────────────────────────────────────────

# Matches "Table 902.02.03-3 HMA Requirements for Design"
_CAPTION_RE = re.compile(
    r'Table\s+([\d]+\.[\d]+(?:\.[\d]+)?(?:-[\d]+)?)\s*(.*)',
    re.IGNORECASE,
)

# Footnote lines begin with "1." / "2." / "*" / "**" / "Note:" etc.
_FOOTNOTE_START_RE = re.compile(
    r'^\s*(?:\d+[.)]\s|[*†‡]\s*|Note[s]?[.:])',
    re.IGNORECASE,
)

# ── pdfplumber table-find settings ───────────────────────────────────────────

_SETTINGS_STRICT = {
    "vertical_strategy":   "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance":      3,
    "join_tolerance":      3,
    "edge_min_length":     3,
}

_SETTINGS_RELAXED = {
    "vertical_strategy":   "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance":      5,
    "join_tolerance":      5,
    "edge_min_length":     3,
}


# ── Main class ────────────────────────────────────────────────────────────────

class TableExtractor:
    """
    Extract and serialize tables from a single pdfplumber page.

    Parameters
    ----------
    caption_scan_pts : float
        How many points above the table top to scan for a caption line.
    footnote_scan_pts : float
        How many points below the table bottom to scan for footnotes.
    """

    def __init__(
        self,
        caption_scan_pts:  float = 60.0,
        footnote_scan_pts: float = 80.0,
    ) -> None:
        self._caption_scan  = caption_scan_pts
        self._footnote_scan = footnote_scan_pts

    # ── Public ────────────────────────────────────────────────────────────────

    def extract_tables(
        self,
        page:          Any,
        page_pdf:      int,
        page_printed:  Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Find all tables on *page* and return a list of table dicts.

        Parameters
        ----------
        page : pdfplumber.page.Page
            A single page from an open ``pdfplumber.PDF``.
        page_pdf : int
            1-based PDF page number (stored in metadata).
        page_printed : int or None
            Printed (human-readable) page number; ``None`` if unknown.

        Returns
        -------
        list[dict]
            One dict per detected table.  Empty list if none found.
        """
        table_objects = self._find_tables(page, page_pdf)
        if not table_objects:
            return []

        results: List[Dict[str, Any]] = []
        for idx, tobj in enumerate(table_objects, start=1):
            try:
                result = self._process_table(
                    page, tobj, idx, page_pdf, page_printed
                )
                if result is not None:
                    results.append(result)
            except Exception as exc:
                # One bad table should never abort the page -- but a table
                # skipped here is a table the retrieval index never sees, so
                # it must not vanish silently.
                logger.warning(
                    "Skipped table %d on PDF page %d: %s", idx, page_pdf, exc,
                )

        return results

    # ── Private: table discovery ──────────────────────────────────────────────

    def _find_tables(self, page: Any, page_pdf: int) -> List[Any]:
        """
        Try strict line-based detection; fall back to relaxed settings.

        Returns pdfplumber Table objects (with .bbox and .extract()).
        """
        try:
            tables = page.find_tables(table_settings=_SETTINGS_STRICT)
            if tables:
                return tables
        except Exception as exc:
            logger.warning(
                "Strict table detection failed on PDF page %d, retrying relaxed: %s",
                page_pdf, exc,
            )

        try:
            tables = page.find_tables(table_settings=_SETTINGS_RELAXED)
            if tables:
                return tables
        except Exception as exc:
            logger.warning(
                "Relaxed table detection also failed on PDF page %d — no tables "
                "extracted from this page: %s",
                page_pdf, exc,
            )

        return []

    # ── Private: per-table processing ────────────────────────────────────────

    def _process_table(
        self,
        page:         Any,
        tobj:         Any,
        idx:          int,
        page_pdf:     int,
        page_printed: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        """Extract, classify and serialise a single table object."""
        raw_rows = tobj.extract()
        if not raw_rows:
            return None

        # Filter out completely empty rows
        raw_rows = [r for r in raw_rows if any(
            (c is not None and str(c).strip() not in ("", "–", "-"))
            for c in r
        )]
        if not raw_rows:
            return None

        bbox: Tuple[float, float, float, float] = tobj.bbox  # (x0, top, x1, bottom)

        row_count = len(raw_rows)
        col_count = max((len(r) for r in raw_rows), default=0)

        table_type = self._classify(raw_rows)
        if table_type == "multi_header" and len(raw_rows) >= 2:
            raw_rows = self._flatten_multi_header(raw_rows)
        markdown   = self._to_markdown(raw_rows)

        table_id, table_title = self._find_caption(page, bbox, page_pdf, idx)
        footnotes             = self._find_footnotes(page, bbox, page_pdf, idx)
        if footnotes:
            markdown += "\n\nNotes:\n" + "\n".join(footnotes)

        return {
            "table_id":    table_id,
            "table_title": table_title,
            "table_type":  table_type,
            "markdown":    markdown,
            "raw_rows":    raw_rows,
            "footnotes":   footnotes,
            "bbox":        bbox,
            "page_pdf":    page_pdf,
            "page_printed": page_printed,
            "row_count":   row_count,
            "col_count":   col_count,
        }

    # ── Private: caption detection ────────────────────────────────────────────

    def _find_caption(
        self,
        page:     Any,
        bbox:     Tuple[float, float, float, float],
        page_pdf: int,
        idx:      int,
    ) -> Tuple[str, str]:
        """
        Crop the zone above the table top and look for a caption line.

        Returns
        -------
        (table_id, table_title)
            Falls back to ``"p<page>_t<idx>"`` / ``""`` when nothing matches.
        """
        x0, top, x1, bottom = bbox
        scan_top = max(0.0, top - self._caption_scan)

        try:
            caption_page = page.crop((0.0, scan_top, page.width, top))
            caption_text = caption_page.extract_text() or ""
        except Exception as exc:
            logger.warning(
                "Caption scan failed for table %d on PDF page %d — the table will "
                "fall back to a positional id: %s",
                idx, page_pdf, exc,
            )
            caption_text = ""

        # Walk lines in reverse (closest to the table wins)
        for line in reversed(caption_text.splitlines()):
            m = _CAPTION_RE.search(line.strip())
            if m:
                return m.group(1).strip(), m.group(2).strip()

        fallback_id = f"p{page_pdf}_t{idx}"
        return fallback_id, ""

    # ── Private: footnote detection ───────────────────────────────────────────

    def _find_footnotes(
        self,
        page: Any,
        bbox: Tuple[float, float, float, float],
        page_pdf: int,
        idx: int,
    ) -> List[str]:
        """
        Crop the zone below the table bottom and collect footnote lines.

        Returns
        -------
        list[str]
            Each element is one footnote line (stripped).
        """
        x0, top, x1, bottom = bbox
        scan_bottom = min(float(page.height), bottom + self._footnote_scan)

        try:
            fn_page = page.crop((0.0, bottom, page.width, scan_bottom))
            fn_text = fn_page.extract_text() or ""
        except Exception as exc:
            logger.warning(
                "Footnote scan failed below table %d on PDF page %d: %s",
                idx, page_pdf, exc,
            )
            return []

        footnotes: List[str] = []
        for line in fn_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if _FOOTNOTE_START_RE.match(stripped):
                footnotes.append(stripped)
            elif footnotes:
                # Continuation of a multi-line footnote
                footnotes[-1] += " " + stripped

        return footnotes

    # ── Private: classification ───────────────────────────────────────────────

    def _classify(self, rows: List[List[Any]]) -> str:
        """
        Classify the table into one of four types.

        Priority (highest first):
            wide_sparse → multi_header → lookup → simple
        """
        if not rows:
            return "simple"

        row_count = len(rows)
        col_count = max((len(r) for r in rows), default=0)

        # ── wide_sparse: ≥10 cols with many empty/dash cells ─────────────────
        if col_count >= 10:
            total = sum(len(r) for r in rows)
            empty = sum(
                1 for r in rows for c in r
                if c is None or str(c).strip() in ("", "–", "-", "—")
            )
            if total > 0 and empty / total >= 0.35:
                return "wide_sparse"

        # ── multi_header: top 1–3 rows have at least one None/empty cell ─────
        if row_count >= 2:
            header_rows = rows[: min(3, row_count - 1)]
            for hr in header_rows:
                none_count = sum(
                    1 for c in hr
                    if c is None or str(c).strip() == ""
                )
                if 0 < none_count < len(hr):
                    return "multi_header"

        # ── lookup: many rows, few columns ────────────────────────────────────
        if row_count >= 10 and 2 <= col_count <= 6:
            return "lookup"

        return "simple"

    # ── Private: multi-header flattening ─────────────────────────────────────

    @staticmethod
    def _flatten_multi_header(rows: List[List[Any]]) -> List[List[Any]]:
        """
        Merge the first two header rows of a multi-header table into one row.

        pdfplumber leaves empty cells where a spanning header cell covers
        multiple columns.  This method propagates each non-empty top-row value
        rightward into the cells it spans, then combines it with the
        second-row sub-column label so the LLM sees a fully-qualified header
        in every column.

        Example
        -------
        Row 0: ["Roadway Type", "Current IRI (C)", "New Construction...", "", "", "", ""]
        Row 1: ["",             "",                "",                   "One⁴","Two⁴","Three⁴","Four or More⁴"]

        Merged: ["Roadway Type", "Current IRI (C)",
                 "New Construction...",
                 "New Construction... — One⁴",
                 "New Construction... — Two⁴",
                 "New Construction... — Three⁴",
                 "New Construction... — Four or More⁴"]
        """
        if len(rows) < 2:
            return rows

        row0 = [str(c).strip() if c is not None else "" for c in rows[0]]
        row1 = [str(c).strip() if c is not None else "" for c in rows[1]]

        width = max(len(row0), len(row1))
        while len(row0) < width:
            row0.append("")
        while len(row1) < width:
            row1.append("")

        merged: List[str] = []
        last_top = ""
        for i in range(width):
            top = row0[i] if row0[i] else last_top
            if row0[i]:
                last_top = row0[i]
            bottom = row1[i]
            if top and bottom and top != bottom:
                merged.append(f"{top} — {bottom}")
            elif top:
                merged.append(top)
            else:
                merged.append(bottom)

        return [merged] + list(rows[2:])

    # ── Private: markdown serialisation ──────────────────────────────────────

    def _to_markdown(self, rows: List[List[Any]]) -> str:
        """
        Render *rows* as a GitHub-flavoured pipe-delimited markdown table.

        * ``None`` cells → empty string
        * All cell values are stripped
        * Short rows are padded to match the header width
        """
        if not rows:
            return ""

        # Normalise: strip + replace None
        def _cell(c: Any) -> str:
            return str(c).strip() if c is not None else ""

        cleaned = [[_cell(c) for c in row] for row in rows]
        width   = max(len(r) for r in cleaned)

        # Pad all rows to the same width
        for row in cleaned:
            while len(row) < width:
                row.append("")

        lines: List[str] = []
        header = cleaned[0]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * width) + "|")

        for row in cleaned[1:]:
            # Replace empty-cell dashes so they survive as explicit "–" markers
            cells = [c if c else "" for c in row]
            lines.append("| " + " | ".join(cells) + " |")

        return "\n".join(lines)
