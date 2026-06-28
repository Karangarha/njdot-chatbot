"""Docling-based table extractor for high-risk NJDOT table types.

WHY THIS EXISTS
---------------
The regex-based table_chunker.py fails on two critical table types:

1. GRADATION tables — wide sieve matrices where empty cells collapse and
   values detach from their row labels under text extraction.

2. MIX_DESIGN -2 VOLUMETRIC tables — the column headers span 3 PDF text
   lines ("Required Density (% of Max Sp.Gr.) / @ Ndes / @ Nmax"), which
   pdftotext collapses into one garbled line. The current chunker then produces
   rows with misaligned values (e.g. reports ESALs when the answer is gyrations).

Docling (IBM, MIT license) uses TableFormer — a vision transformer that
reconstructs table row/column structure from the rendered page image rather than
raw text. This recovers multi-line merged headers and empty-cell alignment.

WHAT THIS SCRIPT DOES
----------------------
1. Converts the full PDF with Docling (this is slow: ~5-10 min first run,
   then cached).
2. Iterates over all detected table objects in the Docling document.
3. For each table, checks if it's one of the high-risk types we want to
   override.
4. Generates NL row-level chunks in the same schema as table_chunks.jsonl.
5. Writes to docling_table_chunks.jsonl (separate file so it can be merged
   selectively into the ingestion pipeline).

INTEGRATION
-----------
ingest_specs.py reads both table_chunks.jsonl (from regex chunker) and
docling_table_chunks.jsonl (from this script). For any table_id that appears
in both, the Docling version takes precedence (it replaces the regex version).

Usage
-----
    # From backend/ directory:
    python scripts/docling_table_chunker.py
    python scripts/docling_table_chunker.py --pdf data/raw_pdfs/StandSpecRoadBridge.pdf
    python scripts/docling_table_chunker.py --tables 902.02.03-2,901.05.02-2
    python scripts/docling_table_chunker.py --debug-table 902.02.03-2
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_BACKEND_DIR  = Path(__file__).resolve().parent.parent
_PDF_PATH     = _BACKEND_DIR / "data" / "raw_pdfs" / "StandSpecRoadBridge.pdf"
_OUTPUT_PATH  = _BACKEND_DIR / "data" / "docling_table_chunks.jsonl"

# ── High-risk table IDs to extract with Docling ───────────────────────────────
#
# These are the tables whose text-parse chunks are wrong or missing in eval.
# Expanding this list is safe — Docling chunks for any table_id listed here
# will replace the regex-chunker version at ingest time.
#
_TARGET_TABLE_IDS: set = {
    # MIX_DESIGN -2 VOLUMETRIC (multi-line merged column headers)
    "902.02.03-2",   # Standard HMA — N_des, N_max, VFA, density → Q5,Q8,Q12,Q13,Q15,Q30
    "902.03.03-2",   # OGFC volumetric
    "902.05.02-2",   # SMA volumetric
    "902.08.02-2",   # HPTO volumetric
    "902.11.03-2",   # BRIC volumetric
    "902.12.02-2",   # ARGG volumetric
    "902.13.03-2",   # HMA HIGH RAP volumetric
    "902.14.02-2",   # BDHMA volumetric
    "902.16.02-2",   # Ultra-HPTO volumetric
    # GRADATION tables (wide sieve matrices)
    "901.05.02-2",   # Fine aggregate HMA gradation → Q3
    "901.10.01-1",   # DGA gradation → Q10
    "901.03-1",      # Standard coarse aggregate sizes
    "902.02.03-1",   # HMA nominal max size grading
    "901.06.02-1",   # Fine aggregate concrete gradation
    "901.11-1",      # Soil aggregate gradations (15 types)
    # MATERIAL_REQUIREMENTS (grouped sub-headers)
    "901.10.02-1",   # RCA composition → Q2, Q27, Q48
    "902.02.02-1",   # Recycled materials in HMA base/intermediate → Q24, Q97
}

# Known table names for IDs where Docling may not parse the caption
_TABLE_NAMES: Dict[str, str] = {
    "902.02.03-2": "Gyratory Compaction Effort and Volumetric Requirements for HMA Mixtures",
    "901.05.02-2": "Quality Requirements for Fine Aggregate Used in Hot Mix Asphalt",
    "901.10.01-1": "Gradation Requirements for Dense-Graded Aggregate",
    "901.03-1":    "Standard Sizes of Coarse Aggregate",
    "902.02.03-1": "JMF Master Ranges for HMA Mixtures",
    "901.06.02-1": "Gradation Requirements for Fine Aggregate Used in Concrete, Mortar, and Grout",
    "901.11-1":    "Standard Soil Aggregate Gradations",
    "901.10.02-1": "Composition Requirements for Recycled Concrete Aggregate (RCA)",
    "902.02.02-1": "Recycled Material Content Requirements for HMA Mixtures",
}

# ── Table caption detection regex (matches Docling caption strings) ────────────
_CAPTION_RE = re.compile(
    r'Table\s+(\d{2,4}\.\d{2}(?:\.\d{2})?(?:\.\d{2})?(?:-\d+)?[A-Z]?)'
    r'(?:\s+(.+))?',
    re.IGNORECASE,
)


def _section_from_table_id(table_id: str) -> str:
    return re.sub(r'[-–]\d+[A-Z]?$', '', table_id)


def _division_from_table_id(table_id: str) -> str:
    return table_id.split(".")[0] if "." in table_id else table_id[:3]


def _make_chunk(
    table_id:   str,
    table_name: str,
    page_num:   int,
    row_idx:    int,
    row_key:    str,
    col_key:    Optional[str],
    value:      str,
    nl_text:    str,
    raw_row:    List[str],
    footnotes:  List[str] = (),
    table_type: str = "unknown",
) -> Dict[str, Any]:
    footnote_strs = [f"[FOOTNOTE]: {fn}" for fn in footnotes]
    full_text = nl_text
    if footnote_strs:
        full_text += "\n" + "\n".join(footnote_strs)

    col_suffix = f"-col-{col_key[:20].replace(' ','_')}" if col_key else ""
    chunk_id   = f"dtbl-{table_id}-row-{row_idx}{col_suffix}"

    return {
        "chunk_id":    chunk_id,
        "chunk_type":  "table_row",
        "table_id":    table_id,
        "table_name":  table_name,
        "section":     _section_from_table_id(table_id),
        "division":    _division_from_table_id(table_id),
        "page":        page_num,
        "table_type":  table_type,
        "row_key":     row_key,
        "col_key":     col_key,
        "footnotes":   list(footnotes),
        "text":        full_text,
        "raw_row":     raw_row,
        "source":      "docling",
    }


# ── Docling conversion ────────────────────────────────────────────────────────

def convert_pdf_with_docling(pdf_path: Path) -> Any:
    """Run Docling on the PDF and return the DoclingDocument."""
    try:
        from docling.document_converter import DocumentConverter
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
    except ImportError:
        print("ERROR: docling is not installed. Run: pip install docling")
        sys.exit(1)

    print(f"Converting PDF with Docling: {pdf_path}")
    print("  (First run downloads TableFormer model weights — ~200MB, takes a few minutes)")

    pipeline_options = PdfPipelineOptions(
        do_table_structure=True,
        do_ocr=False,          # PDF has embedded text — no need for OCR
    )

    converter = DocumentConverter()
    result = converter.convert(str(pdf_path))
    doc = result.document
    print(f"  Docling conversion complete. Found {len(doc.tables)} table objects.")
    return doc


# ── Table caption → ID extraction ─────────────────────────────────────────────

def extract_table_id_from_caption(caption: str) -> Optional[str]:
    """Parse a Docling table caption string and return the table ID."""
    if not caption:
        return None
    m = _CAPTION_RE.search(caption)
    return m.group(1).strip() if m else None


def extract_table_name_from_caption(caption: str) -> str:
    """Return the title portion after the table ID in a caption string."""
    if not caption:
        return ""
    m = _CAPTION_RE.search(caption)
    if m and m.group(2):
        return m.group(2).strip()
    return caption.strip()


# ── Convert a Docling table to a pandas-like grid ────────────────────────────

def table_to_grid(docling_table: Any) -> Tuple[List[List[str]], int]:
    """
    Extract a Docling TableItem into a 2D list of strings and page number.

    Returns (grid, page_num) where grid[0] is the first row.
    Merges multi-row/col span cells by repeating the value.
    """
    try:
        # Docling TableItem.export_to_dataframe() returns a pandas DataFrame
        df = docling_table.export_to_dataframe()
        grid = [list(df.columns)] + df.values.tolist()
        grid = [[str(c) if c is not None else "" for c in row] for row in grid]
        # Get page number
        page_num = 1
        try:
            prov = docling_table.prov
            if prov:
                page_num = prov[0].page_no
        except Exception:
            pass
        return grid, page_num
    except Exception:
        # Fallback: use the raw data array
        try:
            grid = []
            for row in docling_table.data.grid:
                grid.append([str(cell.text) if cell else "" for cell in row])
            page_num = 1
            try:
                page_num = docling_table.prov[0].page_no
            except Exception:
                pass
            return grid, page_num
        except Exception as e:
            return [], 1


# ── Chunk generators for each high-risk type ─────────────────────────────────

def chunks_from_gradation_grid(
    table_id: str, table_name: str, page_num: int, grid: List[List[str]]
) -> List[Dict[str, Any]]:
    """
    GRADATION: wide sieve matrix.
    Row 0 = sieve size headers. Rows 1+ = (size_name, val1, val2, ...).
    One chunk per aggregate row listing all sieve percentages.
    """
    if len(grid) < 2 or len(grid[0]) < 3:
        return []

    # First row = sieve column headers (skip first col which is size designation)
    # Some tables have 2 header rows (No. + size); detect by checking if row 1
    # is also non-numeric
    sieve_headers = [h.strip() for h in grid[0][1:]]

    # If the first column header is empty/generic, the actual sieve names
    # may be in row 0 starting from col 1
    row_label_col = 0  # column index for row key

    chunks = []
    for row_idx, row in enumerate(grid[1:], start=1):
        if not any(c.strip() for c in row):
            continue

        row_key = row[row_label_col].strip()
        if not row_key or row_key in ("", "-", "–"):
            continue

        # Build sieve list
        sieve_parts = []
        for col_idx, sieve in enumerate(sieve_headers):
            if col_idx + 1 >= len(row):
                break
            val = row[col_idx + 1].strip()
            if val and val not in ("-", "–", "—", "", "N/A", "nan"):
                sieve_parts.append(f"{sieve}: {val}%")

        if not sieve_parts:
            continue

        nl = (
            f"In Table {table_id} ({table_name}), "
            f"for {row_key}, "
            f"the gradation (percent passing) is: {', '.join(sieve_parts)}."
        )
        chunks.append(_make_chunk(
            table_id, table_name, page_num, row_idx,
            row_key, None, "; ".join(sieve_parts), nl, row,
            table_type="gradation",
        ))

    return chunks


def chunks_from_volumetric_grid(
    table_id: str, table_name: str, page_num: int, grid: List[List[str]]
) -> List[Dict[str, Any]]:
    """
    MIX_DESIGN -2 VOLUMETRIC: multi-line merged column headers.
    Column structure (after Docling reconstructs headers):
      Aggregate Size | N_des | N_max | Required Density @Ndes | Required Density @Nmax
      | VMA | VFA | Dust-to-Binder | Draindown

    One chunk per aggregate size row per property column.
    """
    if len(grid) < 2:
        return []

    # Docling returns merged headers as a single string in the first row
    headers = [h.strip() for h in grid[0]]

    chunks = []
    for row_idx, row in enumerate(grid[1:], start=1):
        if not any(c.strip() for c in row):
            continue

        row_key = row[0].strip() if row else ""
        if not row_key or row_key in ("", "-", "–", "nan"):
            continue

        for col_idx, header in enumerate(headers[1:], start=1):
            if col_idx >= len(row):
                break
            val = row[col_idx].strip()
            if not val or val in ("-", "–", "—", "", "N/A", "nan"):
                continue

            nl = (
                f"In Table {table_id} ({table_name}), "
                f"for {row_key} aggregate, "
                f"the {header} is {val}."
            )
            chunks.append(_make_chunk(
                table_id, table_name, page_num, row_idx,
                row_key, header, val, nl, row,
                table_type="mix_design_volumetric",
            ))

    return chunks


def chunks_from_material_req_grid(
    table_id: str, table_name: str, page_num: int, grid: List[List[str]]
) -> List[Dict[str, Any]]:
    """
    MATERIAL_REQUIREMENTS: rows are material/property, columns are limits.
    Preserves grouped sub-header rows (e.g. 'Concrete', 'HMA', 'Brick') as
    prefix on subsequent data rows.

    One chunk per data row with all column values inlined.
    """
    if len(grid) < 2:
        return []

    headers = [h.strip() for h in grid[0]]
    chunks = []
    current_group = ""

    for row_idx, row in enumerate(grid[1:], start=1):
        if not any(c.strip() for c in row):
            continue

        row_key = row[0].strip() if row else ""
        if not row_key:
            continue

        # Detect group header rows: single non-empty cell spanning all cols
        non_empty = [c.strip() for c in row if c.strip()]
        if len(non_empty) == 1:
            current_group = row_key
            continue

        # Build NL sentence
        props = []
        for col_idx, header in enumerate(headers[1:], start=1):
            if col_idx >= len(row):
                break
            val = row[col_idx].strip()
            if val and val not in ("-", "–", "—", "", "N/A", "nan"):
                props.append(f"{header}: {val}")

        if not props:
            continue

        prefix = f"{current_group} — " if current_group else ""
        nl = (
            f"In Table {table_id} ({table_name}), "
            f"for {prefix}{row_key}: {'; '.join(props)}."
        )
        chunks.append(_make_chunk(
            table_id, table_name, page_num, row_idx,
            f"{prefix}{row_key}", None,
            "; ".join(props), nl, row,
            table_type="material_requirements",
        ))

    return chunks


def chunks_from_generic_grid(
    table_id: str, table_name: str, page_num: int, grid: List[List[str]]
) -> List[Dict[str, Any]]:
    """
    Generic fallback: one chunk per (row, column) cell with a non-empty value.
    """
    if len(grid) < 2:
        return []

    headers = [h.strip() for h in grid[0]]
    chunks = []

    for row_idx, row in enumerate(grid[1:], start=1):
        if not any(c.strip() for c in row):
            continue
        row_key = row[0].strip() if row else ""
        if not row_key:
            continue

        for col_idx, header in enumerate(headers[1:], start=1):
            if col_idx >= len(row):
                break
            val = row[col_idx].strip()
            if not val or val in ("-", "–", "—", "", "N/A", "nan"):
                continue

            nl = (
                f"In Table {table_id} ({table_name}), "
                f"for {row_key}, "
                f"the {header} is {val}."
            )
            chunks.append(_make_chunk(
                table_id, table_name, page_num, row_idx,
                row_key, header, val, nl, row,
                table_type="generic",
            ))

    return chunks


# ── Table type dispatcher ─────────────────────────────────────────────────────

def _detect_table_type(table_id: str, headers: List[str]) -> str:
    """Guess table type from ID suffix and column headers."""
    # Volumetric tables always end in -2 and have gyration/density headers
    if table_id.endswith("-2") and re.search(r'N.?des|N.?max|VFA|Density|Gyrat', " ".join(headers), re.I):
        return "volumetric"

    # Gradation tables have sieve-like headers
    if re.search(r'No\.\s*\d+|\d+/\d+["\']|sieve|Passing|4"|3"', " ".join(headers), re.I):
        return "gradation"

    # RCA / recycled material composition
    if re.search(r'composit|recycl|concrete|minimum|maximum\s+%', " ".join(headers), re.I):
        return "material_req"

    # Recycled material content tables
    if re.search(r'RAP|GBSM|CRCG|recycled.*content|base.*course', " ".join(headers), re.I):
        return "material_req"

    return "generic"


def generate_chunks_from_docling_table(
    docling_table: Any,
    target_ids: set,
    debug_table_id: Optional[str] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Process one Docling table object. Returns (table_id, chunks).
    Returns ("", []) if the table is not in target_ids.
    """
    # Get caption
    caption = ""
    try:
        cap_obj = docling_table.caption_text(docling_table._parent)
        caption = str(cap_obj).strip()
    except Exception:
        pass

    if not caption:
        try:
            for item in (docling_table.captions or []):
                caption += str(item).strip() + " "
            caption = caption.strip()
        except Exception:
            pass

    table_id   = extract_table_id_from_caption(caption)
    table_name = _TABLE_NAMES.get(table_id, extract_table_name_from_caption(caption))

    if not table_id or (table_id not in target_ids and debug_table_id != table_id):
        return "", []

    grid, page_num = table_to_grid(docling_table)
    if not grid:
        return table_id, []

    if debug_table_id == table_id:
        print(f"\n=== DEBUG TABLE {table_id} ===")
        print(f"  Caption: {caption}")
        print(f"  Page: {page_num}")
        print(f"  Grid ({len(grid)} rows × {len(grid[0]) if grid else 0} cols):")
        for i, row in enumerate(grid[:8]):
            print(f"    Row {i}: {row}")
        if len(grid) > 8:
            print(f"    ... and {len(grid)-8} more rows")

    headers = grid[0] if grid else []
    ttype = _detect_table_type(table_id, headers)

    if ttype == "gradation":
        chunks = chunks_from_gradation_grid(table_id, table_name, page_num, grid)
    elif ttype == "volumetric":
        chunks = chunks_from_volumetric_grid(table_id, table_name, page_num, grid)
    elif ttype == "material_req":
        chunks = chunks_from_material_req_grid(table_id, table_name, page_num, grid)
    else:
        chunks = chunks_from_generic_grid(table_id, table_name, page_num, grid)

    return table_id, chunks


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Docling-based NJDOT table extractor")
    parser.add_argument("--pdf",          default=str(_PDF_PATH),   help="Path to PDF")
    parser.add_argument("--output",       default=str(_OUTPUT_PATH), help="Output JSONL path")
    parser.add_argument("--tables",       default="",                help="Comma-separated table IDs to extract (default: all high-risk)")
    parser.add_argument("--debug-table",  default="",                help="Print raw Docling grid for this table ID and exit")
    args = parser.parse_args()

    pdf_path    = Path(args.pdf)
    output_path = Path(args.output)

    if not pdf_path.exists():
        print(f"ERROR: PDF not found: {pdf_path}")
        sys.exit(1)

    # Determine target table IDs
    if args.tables:
        target_ids = set(t.strip() for t in args.tables.split(","))
    elif args.debug_table:
        target_ids = {args.debug_table}
    else:
        target_ids = _TARGET_TABLE_IDS

    print(f"Target table IDs ({len(target_ids)}): {sorted(target_ids)}")

    # Convert PDF
    doc = convert_pdf_with_docling(pdf_path)

    # Process each table
    all_chunks: List[Dict[str, Any]] = []
    found_ids: List[str] = []

    for tbl in doc.tables:
        table_id, chunks = generate_chunks_from_docling_table(
            tbl, target_ids, debug_table_id=args.debug_table or None
        )
        if table_id and chunks:
            all_chunks.extend(chunks)
            found_ids.append(table_id)
            print(f"  Table {table_id}: {len(chunks)} chunks")

    # Report missing
    missing = sorted(target_ids - set(found_ids))
    if missing:
        print(f"\nWARNING: {len(missing)} target tables not found in Docling output:")
        for tid in missing:
            print(f"  {tid}")

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for chunk in all_chunks:
            f.write(json.dumps(chunk, ensure_ascii=False) + "\n")

    print(f"\nWrote {len(all_chunks)} chunks to {output_path}")
    print(f"Tables extracted: {len(found_ids)}/{len(target_ids)}")

    # Quick spot-check
    for target_id in ["902.02.03-2", "901.05.02-2", "901.10.01-1", "901.10.02-1"]:
        target_chunks = [c for c in all_chunks if c["table_id"] == target_id]
        if target_chunks:
            print(f"\n--- Table {target_id} ({len(target_chunks)} chunks) ---")
            for c in target_chunks[:4]:
                print(f"  [{c['row_key']}]  {c['text'][:120]}")


if __name__ == "__main__":
    main()
