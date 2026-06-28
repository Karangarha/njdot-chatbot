"""Ingestion script for NJDOT PDF documents.

Orchestrates the full pipeline for three document collections:

    specs_2019      StandSpecRoadBridge.pdf          (doc_type="specs")
    scheduling      constructionschedulingmanual.pdf  (doc_type="scheduling")
    material_procs  MP1-25.pdf … MP31-25.pdf          (doc_type="material_proc")

Pipeline per document
---------------------
``specs_2019``  (enhanced pipeline)
    pdfplumber open → per-page TableExtractor → text-without-tables via
    outside_bbox → Chunker(text chunks) + table chunks + table_row chunks
    → embed → insert

All other doc types
    PDFParser → Chunker → embed → insert

Safety
------
Before processing a document, the script checks whether any rows already
exist in the ``chunks`` table with ``metadata->>'doc' = <doc_name>``.
If rows exist the document is skipped so re-running is idempotent.
Use ``--fresh --collection <name>`` to delete existing chunks first.

Supabase row shape
------------------
    content    TEXT          – chunk text
    embedding  VECTOR(1536)  – OpenAI embedding
    collection TEXT          – "specs_2019" | "scheduling" | "material_procs"
    metadata   JSONB         – see below

Metadata JSON shape
--------------------
Standard fields (all chunk kinds):
    {
        "doc":           "Spec2019",
        "section_id":    "902.02.03",
        "section_title": "Mix Design",
        "division":      "DIVISION 900",
        "page_pdf":      441,
        "page_printed":  407,
        "kind":          "text" | "table" | "table_row"
    }

Additional fields for table and table_row chunks:
        "table_id":   "902.02.03-3",
        "table_type": "simple" | "lookup" | "wide_sparse" | "multi_header",
        "footnotes":  ["1. Note text …", "* See …"]

Usage
-----
    # Full ingestion (all documents):
    python scripts/ingest_specs.py

    # Only a specific collection:
    python scripts/ingest_specs.py --collection material_procs

    # Delete existing and re-ingest one collection:
    python scripts/ingest_specs.py --fresh --collection specs_2019

    # Dry-run: embed 3 scheduling-manual chunks, print results, NO DB insert:
    python scripts/ingest_specs.py --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pdfplumber

# ── Put the backend/ package root on sys.path so app.* imports work ───────────
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.config   import config                               # noqa: E402
from app.database import get_db                               # noqa: E402
from app.ingestion.pdf_parser      import PDFParser           # noqa: E402
from app.ingestion.chunker         import Chunker, FRONT_MATTER_PAGES  # noqa: E402
from app.ingestion.embedder        import Embedder             # noqa: E402
from app.ingestion.table_extractor import TableExtractor       # noqa: E402


# ── Constants ─────────────────────────────────────────────────────────────────

DB_INSERT_BATCH:     int = 50
# lookup tables with > this many rows also get per-row chunks (kind="table_row")
TABLE_ROW_THRESHOLD: int = 50

# ── Static document registry ─────────────────────────────────────────────────
#
# Each entry drives one full parse→chunk→embed→insert cycle.
# MP files are added dynamically in _build_doc_configs().

_STATIC_DOCS: list[dict[str, str]] = [
    {
        "filename":   "StandSpecRoadBridge.pdf",
        "doc":        "Spec2019",
        "doc_type":   "specs",
        "collection": "specs_2019",
    },
    {
        "filename":   "constructionschedulingmanual.pdf",
        "doc":        "SchedulingManual",
        "doc_type":   "scheduling",
        "collection": "scheduling",
    },
]


def _build_doc_configs(raw_pdfs_dir: Path) -> list[dict[str, str]]:
    """
    Return the full list of document configs: static entries plus one entry
    per ``MP*.pdf`` file discovered in *raw_pdfs_dir*.
    """
    configs = list(_STATIC_DOCS)
    for mp_path in sorted(raw_pdfs_dir.glob("MP*.pdf")):
        configs.append({
            "filename":   mp_path.name,
            "doc":        mp_path.stem,   # e.g. "MP1-25"
            "doc_type":   "material_proc",
            "collection": "material_procs",
        })
    return configs


def _resolve_collection(target: str, all_configs: list[dict[str, str]]) -> Optional[str]:
    """
    Map a target collection name to the canonical collection name used in
    ``_STATIC_DOCS`` / MP configs.

    Enables versioned destination names like ``"specs_2019_v2"`` to resolve
    to the ``"specs_2019"`` document set without requiring an exact match.

    Strategy: return the longest canonical name that is a **prefix** of
    *target*.  Longest-first avoids ``"specs"`` accidentally matching
    ``"specs_2019"`` if both existed.

    Returns ``None`` when no canonical collection is a prefix of *target*
    (i.e. the name is entirely unknown).

    Examples
    --------
    >>> _resolve_collection("specs_2019_v2", configs)
    'specs_2019'
    >>> _resolve_collection("specs_2019", configs)
    'specs_2019'
    >>> _resolve_collection("material_procs_test", configs)
    'material_procs'
    """
    canonical = sorted(
        {cfg["collection"] for cfg in all_configs},
        key=len,
        reverse=True,   # longest first — avoids short-prefix false matches
    )
    for base in canonical:
        if target == base or target.startswith(base + "_"):
            return base
    return None


# ── Database helpers ──────────────────────────────────────────────────────────

def _already_ingested(db_client: Any, doc_name: str, collection: str) -> bool:
    """
    Return True if the ``chunks`` table already contains rows whose
    ``metadata->>'doc'`` equals *doc_name* **and** whose ``collection``
    column equals *collection*.

    Scoping by collection ensures that a document already present in
    ``specs_2019`` does NOT block a fresh ingest into ``specs_2019_v2``.
    """
    result = (
        db_client.table("chunks")
        .select("id")
        .filter("metadata->>doc", "eq", doc_name)
        .eq("collection", collection)
        .limit(1)
        .execute()
    )
    return len(result.data) > 0


def _delete_collection(db_client: Any, collection_name: str) -> None:
    """
    Delete every row from ``chunks`` where ``collection = collection_name``.

    Called when ``--fresh`` is supplied.  Prints the number of deleted rows.
    """
    result = (
        db_client.table("chunks")
        .delete()
        .eq("collection", collection_name)
        .execute()
    )
    deleted = len(result.data) if result.data else 0
    print(f"  Deleted {deleted} existing rows from collection={collection_name!r}")


def _build_rows(
    chunks: List[Dict[str, Any]],
    doc_name: str,
    collection: str,
) -> List[Dict[str, Any]]:
    """
    Convert embedded chunk dicts into Supabase row dicts.

    The ``doc`` key is injected into metadata here (the chunker does not
    know which document it belongs to).  The ``collection`` is set as a
    top-level column, not inside metadata.

    Table-specific optional fields (``table_id``, ``table_type``,
    ``footnotes``) are passed through when present in the chunk metadata.
    """
    rows = []
    for chunk in chunks:
        meta = {
            "doc":           doc_name,
            "section_id":    chunk["metadata"]["section_id"],
            "section_title": chunk["metadata"]["section_title"],
            "division":      chunk["metadata"]["division"],
            "page_pdf":      chunk["metadata"]["page_pdf"],
            "page_printed":  chunk["metadata"]["page_printed"],
            "kind":          chunk["metadata"]["kind"],
        }
        # Optional table-specific fields
        for key in ("table_id", "table_type", "footnotes"):
            if key in chunk["metadata"]:
                meta[key] = chunk["metadata"][key]

        rows.append({
            "content":    chunk["content"],
            "embedding":  chunk["embedding"],
            "metadata":   meta,
            "collection": collection,
        })
    return rows


def _insert_in_batches(
    db_client: Any,
    rows: List[Dict[str, Any]],
    batch_size: int = DB_INSERT_BATCH,
) -> None:
    """Insert *rows* into the ``chunks`` table in batches of *batch_size*."""
    total = len(rows)
    for start in range(0, total, batch_size):
        end   = min(start + batch_size, total)
        batch = rows[start:end]
        db_client.table("chunks").insert(batch).execute()
        print(f"  Inserted rows {start + 1}-{end} / {total}")


# ── Table extraction helpers ──────────────────────────────────────────────────

_BARE_INT_RE = re.compile(r'^\d+$')


def _printed_page_from_lines(lines: List[str], pdf_page_num: int, front_matter: int) -> int:
    """
    Extract the printed (human-readable) page number from page lines.

    Mirrors the logic of ``chunker._extract_printed_page``:
    last non-empty line that is a bare integer → printed page;
    otherwise fall back to ``pdf_page_num − front_matter`` (≥ 1).
    """
    for line in reversed(lines):
        stripped = line.strip()
        if stripped:
            if _BARE_INT_RE.match(stripped):
                return int(stripped)
            break
    return max(1, pdf_page_num - front_matter)


def _section_id_from_table_id(table_id: str) -> str:
    """
    Extract the section_id portion from a table_id.

    Examples
    --------
    >>> _section_id_from_table_id("902.02.03-3")
    '902.02.03'
    >>> _section_id_from_table_id("p441_t1")
    'p441_t1'
    """
    m = re.match(r'^([\d]+\.[\d]+(?:\.[\d]+)?)(?:-\d+)?$', table_id)
    return m.group(1) if m else table_id


def _extract_text_and_tables(
    pdf_path: Path,
    front_matter_pages: int,
    table_extractor: TableExtractor,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Open *pdf_path* with pdfplumber and process every page.

    For each page:

    1. Extract the full text (used only to derive the printed page number).
    2. Run ``TableExtractor`` to find tables; set ``page_printed`` on each.
    3. Re-extract text with all table bounding boxes excluded via
       ``page.outside_bbox(bbox)`` so table content is not duplicated in
       text chunks.

    Parameters
    ----------
    pdf_path : Path
        Absolute path to the PDF file.
    front_matter_pages : int
        Number of leading pages to skip when computing the fallback printed
        page number (e.g. 34 for specs).
    table_extractor : TableExtractor
        Pre-constructed extractor instance (shared across calls).

    Returns
    -------
    (pages, table_dicts)
        pages       – list of ``{"page_num", "text", "char_count",
                      "extractor"}`` dicts compatible with ``Chunker.chunk()``
        table_dicts – flat list of table dicts from ``TableExtractor``
    """
    pages:      List[Dict[str, Any]] = []
    all_tables: List[Dict[str, Any]] = []

    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            # ── 1. Full text for printed-page-number detection ──────────────
            full_text  = page.extract_text() or ""
            full_lines = full_text.splitlines()
            printed    = _printed_page_from_lines(full_lines, i, front_matter_pages)

            # ── 2. Table extraction ─────────────────────────────────────────
            tables = table_extractor.extract_tables(
                page, page_pdf=i, page_printed=printed
            )
            all_tables.extend(tables)

            # ── 3. Text extraction with table regions removed ───────────────
            remaining = page
            for tbl in tables:
                try:
                    remaining = remaining.outside_bbox(tbl["bbox"])
                except Exception:
                    pass  # keep whatever we have if outside_bbox fails
            try:
                clean_text = remaining.extract_text() or ""
            except Exception:
                clean_text = full_text   # fall back to full text

            pages.append({
                "page_num":  i,
                "text":      clean_text,
                "char_count": len(clean_text),
                "extractor": "pdfplumber",
            })

    return pages, all_tables


def _build_table_chunks(
    table_dicts: List[Dict[str, Any]],
    text_chunks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Convert table dicts into chunk dicts with ``kind="table"``.

    Content format::

        Table 902.02.03-3 HMA Mixture Design Properties
        | col1 | col2 | … |
        |------|------|---|
        | …    | …    |   |
        1. Footnote text …

    Division is inferred from the nearest preceding text chunk by ``page_pdf``.

    Parameters
    ----------
    table_dicts : list[dict]
        Output of ``TableExtractor.extract_tables()``.
    text_chunks : list[dict]
        Already-produced text chunks (used only to look up division by page).

    Returns
    -------
    list[dict]
        Chunk dicts with ``content`` and ``metadata``.
    """
    # Build a sorted list of (page_pdf, division) pairs from text chunks
    # so we can find which division was active at any given page.
    page_divs: List[Tuple[int, str]] = sorted(
        {(c["metadata"]["page_pdf"], c["metadata"].get("division", ""))
         for c in text_chunks},
        key=lambda x: x[0],
    )

    def _division_at(page_pdf: int) -> str:
        """Return the division active at *page_pdf* (last one started before it)."""
        div = ""
        for p, d in page_divs:
            if p <= page_pdf:
                div = d
            else:
                break
        return div

    chunks: List[Dict[str, Any]] = []
    for tbl in table_dicts:
        table_id   = tbl["table_id"]
        section_id = _section_id_from_table_id(table_id)
        division   = _division_at(tbl["page_pdf"])

        # Build the caption line
        caption = f"Table {table_id}"
        if tbl.get("table_title"):
            caption += f" {tbl['table_title']}"

        # Assemble content: caption + markdown + footnotes
        content_lines = [caption, tbl["markdown"]]
        for fn in tbl.get("footnotes") or []:
            content_lines.append(fn)
        content = "\n".join(content_lines)

        chunks.append({
            "content": content,
            "metadata": {
                "section_id":    section_id,
                "section_title": tbl.get("table_title") or f"Table {table_id}",
                "division":      division,
                "page_pdf":      tbl["page_pdf"],
                "page_printed":  tbl.get("page_printed"),
                "kind":          "table",
                "table_id":      table_id,
                "table_type":    tbl.get("table_type", "simple"),
                "footnotes":     tbl.get("footnotes") or [],
            },
        })

    return chunks


def _load_precomputed_table_chunks(
    jsonl_path: Path,
    doc_name: str,
) -> List[Dict[str, Any]]:
    """
    Load pre-computed row-level table chunks from table_chunks.jsonl
    (produced by scripts/table_chunker.py) and convert to the standard
    chunk dict format expected by the embedder.

    These chunks contain richer natural-language sentences than the
    pipe-delimited markdown rows produced by _build_table_row_chunks().
    """
    if not jsonl_path.exists():
        print(f"  -- table_chunks.jsonl not found at {jsonl_path} — skipping")
        return []

    _DIV_MAP = {
        "1": "DIVISION 100", "100": "DIVISION 100",
        "2": "DIVISION 200", "200": "DIVISION 200",
        "3": "DIVISION 300", "300": "DIVISION 300",
        "4": "DIVISION 400", "400": "DIVISION 400",
        "5": "DIVISION 500", "500": "DIVISION 500",
        "6": "DIVISION 600", "600": "DIVISION 600",
        "7": "DIVISION 700", "700": "DIVISION 700",
        "8": "DIVISION 800", "800": "DIVISION 800",
        "9": "DIVISION 900", "900": "DIVISION 900",
        "10": "DIVISION 1000", "1000": "DIVISION 1000",
    }

    import json as _json
    chunks: List[Dict[str, Any]] = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = _json.loads(line)
            except Exception:
                continue

            text = row.get("text", "").strip()
            if not text:
                continue

            div_key = str(row.get("division", "")).split(".")[0]
            division = _DIV_MAP.get(div_key, f"DIVISION {div_key}")

            chunks.append({
                "content": text,
                "metadata": {
                    "doc":           doc_name,
                    "section_id":    row.get("section", ""),
                    "section_title": row.get("table_name", ""),
                    "division":      division,
                    "page_pdf":      row.get("page"),
                    "page_printed":  row.get("page"),
                    "kind":          "table_row",
                    "table_id":      row.get("table_id", ""),
                    "table_type":    row.get("table_type", "simple"),
                    "footnotes":     row.get("footnotes") or [],
                    "row_key":       row.get("row_key", ""),
                    "col_key":       row.get("col_key"),
                    "chunk_id":      row.get("chunk_id", ""),
                },
            })

    return chunks


def _build_table_row_chunks(
    tbl:        Dict[str, Any],
    section_id: str,
    division:   str,
) -> List[Dict[str, Any]]:
    """
    Emit one ``kind="table_row"`` chunk per data row for large lookup tables.

    Content format::

        Table 902.02.03-2 Gradation Requirements
        | Sieve Size | % Passing |
        | 19.0 mm    | 100       |

    Only used when ``tbl["table_type"] == "lookup"`` and
    ``tbl["row_count"] > TABLE_ROW_THRESHOLD``.

    Parameters
    ----------
    tbl : dict
        A single table dict from ``TableExtractor``.
    section_id : str
        Pre-computed section_id (e.g. ``"902.02.03"``).
    division : str
        Division string to embed in metadata.

    Returns
    -------
    list[dict]
        One chunk dict per data row (rows[1:]).  Empty list if < 2 raw rows.
    """
    raw_rows = tbl.get("raw_rows") or []
    if len(raw_rows) < 2:
        return []

    def _cell(c: Any) -> str:
        return str(c).strip() if c is not None else ""

    table_id    = tbl["table_id"]
    caption     = f"Table {table_id}"
    if tbl.get("table_title"):
        caption += f" {tbl['table_title']}"

    header      = raw_rows[0]
    header_line = "| " + " | ".join(_cell(c) for c in header) + " |"

    chunks: List[Dict[str, Any]] = []
    for row in raw_rows[1:]:
        row_line = "| " + " | ".join(_cell(c) for c in row) + " |"
        content  = f"{caption}\n{header_line}\n{row_line}"
        chunks.append({
            "content": content,
            "metadata": {
                "section_id":    section_id,
                "section_title": tbl.get("table_title") or f"Table {table_id}",
                "division":      division,
                "page_pdf":      tbl["page_pdf"],
                "page_printed":  tbl.get("page_printed"),
                "kind":          "table_row",
                "table_id":      table_id,
                "table_type":    tbl.get("table_type", "lookup"),
                "footnotes":     tbl.get("footnotes") or [],
            },
        })
    return chunks


# ── Per-document pipeline ─────────────────────────────────────────────────────

def _ingest_one(
    db_client: Any,
    embedder:  Embedder,
    doc_cfg:   Dict[str, str],
    raw_pdfs_dir: Path,
) -> tuple[int, List[Dict[str, Any]]]:
    """
    Run the full pipeline for one document.

    For ``doc_type="specs"``:
        pdfplumber → TableExtractor → text-without-tables → Chunker
        → table chunks → table_row chunks → embed → insert

    For all other doc types:
        PDFParser → Chunker → embed → insert

    Returns
    -------
    (n_inserted, chunks)
        n_inserted : number of chunks inserted (0 if skipped)
        chunks     : the embedded chunk list (empty if skipped)
    """
    pdf_path   = raw_pdfs_dir / doc_cfg["filename"]
    doc_name   = doc_cfg["doc"]
    collection = doc_cfg["collection"]
    doc_type   = doc_cfg["doc_type"]

    print(f"\n{'─' * 60}")
    print(f"📄 {doc_cfg['filename']}")
    print(f"   doc={doc_name!r}  collection={collection!r}  doc_type={doc_type!r}")

    # ── Guard: file exists? ────────────────────────────────────────────────────
    if not pdf_path.exists():
        print(f"  ⚠️  File not found — skipping.")
        return 0, []

    # ── Guard: already ingested? ───────────────────────────────────────────────
    if _already_ingested(db_client, doc_name, collection):
        print(f"  ⏭️  Already ingested (doc={doc_name!r} in collection={collection!r}) — skipping.")
        return 0, []

    # ── Specs: enhanced pdfplumber + table-extraction pipeline ────────────────
    if doc_type == "specs":
        print(f"  🔍 Running table extraction + text pipeline…")
        extractor = TableExtractor()

        pages, table_dicts = _extract_text_and_tables(
            pdf_path, FRONT_MATTER_PAGES, extractor
        )
        print(f"  📖 Parsed {len(pages)} pages  |  found {len(table_dicts)} table(s)")

        # ── Text chunks ────────────────────────────────────────────────────────
        text_chunks = Chunker(doc_type=doc_type, doc_name=doc_name).chunk(pages)
        print(f"  ✂️  Produced {len(text_chunks)} text chunks")

        # ── Table chunks ───────────────────────────────────────────────────────
        table_chunks = _build_table_chunks(table_dicts, text_chunks)
        print(f"  📋 Produced {len(table_chunks)} table chunks")

        # ── Table-row chunks (large lookup tables only) ────────────────────────
        # Build table_id → division lookup from the table_chunks we just made.
        tbl_division: Dict[str, str] = {
            c["metadata"]["table_id"]: c["metadata"]["division"]
            for c in table_chunks
        }
        row_chunks: List[Dict[str, Any]] = []
        for tbl in table_dicts:
            if (
                tbl.get("table_type") == "lookup"
                and tbl.get("row_count", 0) > TABLE_ROW_THRESHOLD
            ):
                sid = _section_id_from_table_id(tbl["table_id"])
                div = tbl_division.get(tbl["table_id"], "")
                row_chunks.extend(_build_table_row_chunks(tbl, sid, div))
        if row_chunks:
            print(f"  📝 Produced {len(row_chunks)} table_row chunks")

        # ── Pre-computed natural-language row chunks (table_chunker.py) ───────
        tbl_jsonl = _BACKEND / "data" / "table_chunks.jsonl"
        nl_row_chunks = _load_precomputed_table_chunks(tbl_jsonl, doc_name)
        if nl_row_chunks:
            print(f"  📊 Loaded {len(nl_row_chunks)} pre-computed table_row chunks from table_chunks.jsonl")

        # ── Docling-extracted chunks override regex chunks for high-risk tables ─
        docling_jsonl = _BACKEND / "data" / "docling_table_chunks.jsonl"
        docling_chunks = _load_precomputed_table_chunks(docling_jsonl, doc_name)
        if docling_chunks:
            # Remove any regex-chunker rows for table IDs covered by Docling
            docling_ids = {c["metadata"].get("table_id") for c in docling_chunks}
            nl_row_chunks = [c for c in nl_row_chunks
                             if c["metadata"].get("table_id") not in docling_ids]
            nl_row_chunks.extend(docling_chunks)
            print(f"  🔬 Merged {len(docling_chunks)} Docling chunks (overriding {len(docling_ids)} table IDs)")

        chunks = text_chunks + table_chunks + row_chunks + nl_row_chunks

    # ── All other doc types: existing PDFParser → Chunker pipeline ────────────
    else:
        pages = PDFParser(str(pdf_path)).extract_text()
        print(f"  📖 Parsed {len(pages)} pages")

        chunks = Chunker(doc_type=doc_type, doc_name=doc_name).chunk(pages)
        print(f"  ✂️  Produced {len(chunks)} chunks")

    if not chunks:
        print("  ⚠️  No chunks produced — skipping insert.")
        return 0, []

    # ── Embed ──────────────────────────────────────────────────────────────────
    embedder.embed(chunks)

    # ── Build rows ─────────────────────────────────────────────────────────────
    rows = _build_rows(chunks, doc_name, collection)

    # ── Insert ─────────────────────────────────────────────────────────────────
    _insert_in_batches(db_client, rows)
    print(f"  ✅ Inserted {len(rows)} chunks for {doc_name!r}")

    return len(rows), chunks


# ── Main ──────────────────────────────────────────────────────────────────────

def main(
    dry_run:    bool          = False,
    collection: Optional[str] = None,
    fresh:      bool          = False,
) -> None:
    """
    Run the ingestion pipeline.

    Parameters
    ----------
    dry_run : bool
        If True, process only the scheduling manual, skip the DB insert, and
        print the first 3 chunks with embedding-dimension confirmation.
    collection : str or None
        If set, only process documents whose ``collection`` field matches this
        value (e.g. ``"material_procs"``).  ``None`` → process all.
    fresh : bool
        If True, delete all existing chunks for the target collection before
        ingesting.  Requires *collection* to be specified.
    """
    raw_pdfs_dir = Path(config.RAW_PDFS_DIR)
    t_start = time.time()

    # ── Dry-run branch ────────────────────────────────────────────────────────
    if dry_run:
        _dry_run(raw_pdfs_dir)
        return

    # ── Full ingestion ────────────────────────────────────────────────────────
    if not config.validate():
        sys.exit(1)

    db_client   = get_db()
    embedder    = Embedder()
    doc_configs = _build_doc_configs(raw_pdfs_dir)

    # Filter to a single collection if requested
    if collection:
        canonical = _resolve_collection(collection, doc_configs)
        if canonical is None:
            print(
                f"⚠️  Unknown collection {collection!r}. "
                f"Known bases: {sorted({c['collection'] for c in doc_configs})}"
            )
            return
        # Keep only docs whose canonical collection matches, then override
        # the collection field to the user-supplied target name so all
        # inserted rows land in the right destination (e.g. "specs_2019_v2").
        doc_configs = [
            {**c, "collection": collection}
            for c in doc_configs
            if c["collection"] == canonical
        ]

    # ── --fresh: delete existing chunks before ingesting ─────────────────────
    if fresh:
        if not collection:
            print(
                "ERROR: --fresh requires --collection to be specified.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"\n🗑️  --fresh: deleting all existing chunks for collection={collection!r}")
        _delete_collection(db_client, collection)

    print(f"\n🚀 Starting ingestion — {len(doc_configs)} document(s) to process")
    if collection:
        print(f"   (filtered to collection={collection!r})")

    # Accumulate per-collection stats and keep last-inserted MP chunks for preview
    stats: dict[str, int] = {}
    total_inserted = 0
    mp_sample_chunks: List[Dict[str, Any]] = []

    for doc_cfg in doc_configs:
        n, chunks = _ingest_one(db_client, embedder, doc_cfg, raw_pdfs_dir)
        col = doc_cfg["collection"]
        stats[col] = stats.get(col, 0) + n
        total_inserted += n

        # Collect the first 3 inserted MP chunks for the preview below
        if doc_cfg["collection"] == "material_procs" and chunks and len(mp_sample_chunks) < 3:
            mp_sample_chunks.extend(chunks[:3 - len(mp_sample_chunks)])

    elapsed = time.time() - t_start

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print("📊 Ingestion summary")
    print(f"{'=' * 60}")
    for col, count in sorted(stats.items()):
        print(f"  {col:<20} {count:>6} chunks")
    print(f"  {'TOTAL':<20} {total_inserted:>6} chunks")
    print(f"  Time elapsed: {elapsed:.1f}s")
    print(f"{'=' * 60}\n")

    # ── First-3-chunks preview for material_procs ─────────────────────────────
    if mp_sample_chunks:
        print(f"{'─' * 60}")
        print(f"🔍 First {len(mp_sample_chunks)} material_procs chunk(s) preview")
        print(f"{'─' * 60}")
        for i, chunk in enumerate(mp_sample_chunks, start=1):
            meta    = chunk["metadata"]
            preview = " ".join(chunk["content"][:300].split())
            print(f"\n  Chunk {i}")
            print(f"    section_id    : {meta['section_id']!r}")
            print(f"    section_title : {meta['section_title']!r}")
            print(f"    division      : {meta['division']!r}")
            print(f"    page_pdf      : {meta['page_pdf']}")
            print(f"    page_printed  : {meta['page_printed']}")
            print(f"    kind          : {meta['kind']!r}")
            print(f"    content       : {preview!r}…")
        print(f"\n{'─' * 60}\n")


def _dry_run(raw_pdfs_dir: Path) -> None:
    """
    Dry-run: parse + chunk + embed 3 chunks from the scheduling manual.
    Prints metadata and confirms 1536 embedding dimensions.  No DB insert.
    """
    pdf_path   = raw_pdfs_dir / "constructionschedulingmanual.pdf"
    doc_name   = "SchedulingManual"
    collection = "scheduling"

    print("\n🧪 DRY-RUN — constructionschedulingmanual.pdf")
    print("   (no database writes)")
    print("=" * 60)

    if not pdf_path.exists():
        print(f"❌ PDF not found: {pdf_path}")
        sys.exit(1)

    # Parse
    pages = PDFParser(str(pdf_path)).extract_text()
    print(f"  📖 Parsed {len(pages)} pages")

    # Chunk
    chunks = Chunker(doc_type="scheduling").chunk(pages)
    print(f"  ✂️  Produced {len(chunks)} chunks total")

    # Embed only first 3 to keep API cost minimal
    sample = chunks[:3]
    print(f"  🔢 Embedding {len(sample)} sample chunks…\n")
    embedder = Embedder()
    embedder.embed(sample)

    # Preview what the DB rows would look like
    rows = _build_rows(sample, doc_name, collection)

    print()
    print(f"  {'─' * 58}")
    print(f"  First {len(rows)} chunks (would-be DB rows)")
    print(f"  {'─' * 58}")

    for i, (chunk, row) in enumerate(zip(sample, rows), start=1):
        meta = row["metadata"]
        emb  = row["embedding"]
        assert len(emb) == 1536, f"❌ Expected 1536 dims, got {len(emb)}"
        print(f"\n  Chunk {i} of {len(chunks)} total")
        print(f"    collection    : {row['collection']!r}")
        print(f"    doc           : {meta['doc']!r}")
        print(f"    section_id    : {meta['section_id']!r}")
        print(f"    section_title : {meta['section_title']!r}")
        print(f"    division      : {meta['division']!r}")
        print(f"    page_pdf      : {meta['page_pdf']}")
        print(f"    page_printed  : {meta['page_printed']}")
        print(f"    kind          : {meta['kind']!r}")
        print(f"    embedding dim : {len(emb)}  ✅")
        preview = " ".join(chunk["content"][:200].split())
        print(f"    content       : {preview!r}…")

    print(f"\n  {'─' * 58}")
    print(f"  ✅ Dry-run complete — {len(chunks)} chunks ready to ingest.\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest NJDOT PDF documents into Supabase.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Parse + chunk + embed 3 chunks from the scheduling manual only. "
            "Prints results without writing to the database."
        ),
    )
    parser.add_argument(
        "--collection",
        metavar="NAME",
        default=None,
        help=(
            "Only process documents belonging to this collection "
            "(e.g. 'material_procs', 'specs_2019', 'scheduling'). "
            "Omit to process all collections."
        ),
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help=(
            "Delete all existing chunks for the target collection before "
            "ingesting. Requires --collection. Use with caution."
        ),
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run, collection=args.collection, fresh=args.fresh)
