"""
ingest_bdc.py — Ingest BDC (Baseline Document Change) PDFs into Supabase.

For each BDC PDF found in data/raw_pdfs/BDC*.pdf:
  1. Extract header: bdc_id, bdc_date, subject, implementation_code
  2. Split body into amendment blocks by detecting section-ID header lines
  3. For each block:
       - Insert one row into bdc_section_map
       - Embed chunk content + insert into chunks (collection=bdc_updates)

PDF structure (consistent across all 17 BDCs observed):
  Page 1:  ANNOUNCEMENT / DATE / SUBJECT / summary paragraph
  Body:    SECTION_ID Title
           [CHANGE INSTRUCTION — ALL CAPS, ends with colon]
           [replacement/inserted text]
  Last:    Implementation Code R (ROUTINE) | U (URGENT) + signatures

Usage
-----
    python scripts/ingest_bdc.py [--collection bdc_updates] [--fresh] [--dry-run]

    --fresh    Delete all existing BDC data before ingesting
    --dry-run  Show extracted blocks without writing to DB
"""
from __future__ import annotations

import argparse
import re
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pdfplumber
import openai

from app.config   import config
from app.database import get_db

_oai = openai.OpenAI(api_key=config.OPENAI_API_KEY)


# ── Regex patterns ─────────────────────────────────────────────────────────────

# ANNOUNCEMENT: BDC25S-01  (note: ID contains a hyphen, not matched by \w)
_BDC_ID_RE = re.compile(r"ANNOUNCEMENT:\s+(BDC[\w-]+)", re.IGNORECASE)
# DATE: April 3, 2025
_DATE_RE = re.compile(r"DATE:\s+(.+?)(?:\n|$)", re.IGNORECASE)
# Implementation Code R (ROUTINE) or U (URGENT)
_IMPL_CODE_RE = re.compile(r"Implementation\s+Code\s+(R|U)\s*\((ROUTINE|URGENT)\)", re.IGNORECASE)
# SUBJECT: ... (multi-line, until blank line)
_SUBJECT_RE = re.compile(r"SUBJECT:\s*(.+?)(?=\n\n|\Z)", re.DOTALL | re.IGNORECASE)
# Section header: line starting with section ID followed by a title word beginning
# with a letter (e.g. "107.11.01 Risks", "902.16 Ultra-High Performance...")
_SECTION_HDR_RE = re.compile(r"^(\d{3,4}\.\d{2}(?:\.\d{2})?)\s+([A-Za-z].+)$", re.MULTILINE)
# Change instruction: ALL-CAPS line ending with colon
# Examples: "THE ENTIRE SUBPART IS CHANGED TO:"
#           "THE FOLLOWING IS ADDED AFTER THE 1ST PARAGRAPH:"
_CHANGE_INSTR_RE = re.compile(r"^[A-Z][A-Z0-9 ,\-'\"()./]+:$", re.MULTILINE)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _embed(text: str) -> list[float]:
    resp = _oai.embeddings.create(model=config.EMBEDDING_MODEL, input=[text])
    return resp.data[0].embedding


def _parse_date(raw: str) -> date:
    raw = raw.strip()
    for fmt in ("%B %d, %Y", "%B %d,%Y", "%b %d, %Y", "%b. %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {raw!r}")


def _extract_change_type(instruction: str) -> str:
    upper = instruction.upper()
    if "DELETED" in upper or "DELETE" in upper:
        return "deleted"
    if "REPLACED" in upper or "REPLACE" in upper:
        return "replaced"
    if "ADDED" in upper or " ADD " in upper:
        return "added"
    if "CHANGED" in upper or "CHANGE" in upper:
        return "changed"
    return "amended"


# ── PDF parsing ────────────────────────────────────────────────────────────────

def _extract_full_text(pdf_path: Path) -> tuple[str, str]:
    """Return (page1_text, full_text) from a PDF."""
    pages: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    page1 = pages[0] if pages else ""
    return page1, "\n".join(pages)


def _parse_header(page1: str) -> dict[str, Any]:
    """Extract metadata from page 1 of a BDC."""
    bdc_id_m  = _BDC_ID_RE.search(page1)
    bdc_id    = bdc_id_m.group(1) if bdc_id_m else None

    date_m    = _DATE_RE.search(page1)
    bdc_date  = _parse_date(date_m.group(1)) if date_m else None

    impl_m    = _IMPL_CODE_RE.search(page1)
    impl_code = impl_m.group(1).upper() if impl_m else "R"

    subj_m  = _SUBJECT_RE.search(page1)
    subject = ""
    if subj_m:
        raw   = subj_m.group(1)
        lines = [l.strip().lstrip("- ").strip() for l in raw.splitlines()]
        subject = " ".join(l for l in lines if l)[:300]

    effective_date = bdc_date
    if bdc_date and impl_code == "U":
        effective_date = bdc_date + timedelta(days=7)

    return {
        "bdc_id":              bdc_id,
        "bdc_date":            bdc_date,
        "effective_date":      effective_date,
        "implementation_code": impl_code,
        "subject":             subject,
    }


def _split_amendment_blocks(full_text: str) -> list[dict[str, str]]:
    """
    Split BDC body text into amendment blocks.

    Each block is bounded by section header lines (e.g. "107.11.01 Risks").
    Within each block, the first ALL-CAPS-line-ending-in-colon is the change
    instruction; everything after it is the amendment text.
    """
    matches = list(_SECTION_HDR_RE.finditer(full_text))
    blocks: list[dict[str, str]] = []

    for i, m in enumerate(matches):
        section_id    = m.group(1)
        section_title = m.group(2).strip()

        # Skip "Recommended By:", "Approved By:" and similar artifact lines
        lower_title = section_title.lower()
        if any(kw in lower_title for kw in ("recommended", "approved", "signed")):
            continue

        # Block text: from this header to the next header (or end of doc)
        block_start = m.start()
        block_end   = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        block_text  = full_text[block_start:block_end]

        # Remove page-header repetitions (e.g. "BDC25S-03 Page 2 of 5")
        block_text = re.sub(r"BDC\w+ Page \d+ of \d+\n?", "", block_text)

        # Trim "Implementation Code" trailer from last block
        impl_pos = block_text.find("Implementation Code")
        if impl_pos != -1:
            block_text = block_text[:impl_pos]

        # Trim "Recommended By:" signature block
        sig_pos = block_text.find("Recommended By:")
        if sig_pos != -1:
            block_text = block_text[:sig_pos]

        # Find first change instruction (ALL-CAPS line ending with colon)
        instr_m = _CHANGE_INSTR_RE.search(block_text)
        if instr_m:
            change_type    = _extract_change_type(instr_m.group(0))
            amendment_text = block_text[instr_m.end():].strip()
        else:
            # No explicit instruction — skip the header line itself
            nl = block_text.find("\n")
            amendment_text = block_text[nl + 1:].strip() if nl != -1 else ""
            change_type    = "amended"

        # Skip trivially short blocks (noise, cross-references, etc.)
        if len(amendment_text) < 40:
            continue

        blocks.append({
            "section_id":     section_id,
            "section_title":  section_title,
            "change_type":    change_type,
            "amendment_text": amendment_text,
        })

    return blocks


def _build_chunk_content(header: dict[str, Any], block: dict[str, str]) -> str:
    """Format the searchable chunk string for the bdc_updates collection."""
    impl_label = "ROUTINE" if header["implementation_code"] == "R" else "URGENT"
    return (
        f"[{header['bdc_id']} \u2014 Effective {header['effective_date']} \u2014 {impl_label}]\n"
        f"Section {block['section_id']} {block['section_title']}\n"
        f"Amendment type: {block['change_type']}\n\n"
        f"{block['amendment_text']}"
    )


# ── DB operations ──────────────────────────────────────────────────────────────

def _delete_existing(db: Any, collection: str) -> None:
    print("  Deleting existing bdc_section_map rows …", end="", flush=True)
    # Delete all rows — use a condition that always matches
    db.table("bdc_section_map").delete().neq("bdc_chunk_id", "00000000-0000-0000-0000-000000000000").execute()
    print(" done.")
    print("  Deleting existing bdc_updates chunks …", end="", flush=True)
    db.table("chunks").delete().eq("collection", collection).execute()
    print(" done.")


# ── Main ingestion loop ────────────────────────────────────────────────────────

def ingest_bdc(pdf_path: Path, db: Any, collection: str, dry_run: bool) -> int:
    """Ingest one BDC PDF. Returns number of amendment blocks processed."""
    page1, full_text = _extract_full_text(pdf_path)
    header = _parse_header(page1)

    if not header["bdc_id"]:
        print(f"  ⚠️  {pdf_path.name}: could not parse BDC ID — skipping.")
        return 0

    impl_label = "ROUTINE" if header["implementation_code"] == "R" else "URGENT"
    print(f"\n  [{header['bdc_id']}]  {header['bdc_date']}  {impl_label}")
    print(f"  Subject: {header['subject'][:80]}")

    blocks = _split_amendment_blocks(full_text)
    if not blocks:
        print(f"  ⚠️  No amendment blocks detected.")
        return 0

    for block in blocks:
        print(f"    → {block['section_id']:<14} ({block['change_type']:<10})  "
              f"{len(block['amendment_text'])} chars")

        if dry_run:
            continue

        # Insert chunk first — bdc_section_map.bdc_chunk_id is a FK to chunks.id
        content   = _build_chunk_content(header, block)
        embedding = _embed(content)
        chunk_res = db.table("chunks").insert({
            "collection": collection,
            "content":    content,
            "embedding":  embedding,
            "metadata": {
                "doc":                 header["bdc_id"],
                "bdc_id":              header["bdc_id"],
                "bdc_date":            str(header["bdc_date"]),
                "effective_date":      str(header["effective_date"]),
                "implementation_code": header["implementation_code"],
                "section_id":          block["section_id"],
                "section_title":       block["section_title"],
                "change_type":         block["change_type"],
                "kind":                "bdc_amendment",
            },
        }).execute()
        chunk_id = chunk_res.data[0]["id"]

        # Row in bdc_section_map — bdc_chunk_id = FK to chunks.id just inserted;
        # section_prefix has NOT NULL constraint, derive from section_id
        section_prefix = block["section_id"].split(".")[0]
        db.table("bdc_section_map").insert({
            "bdc_chunk_id":        chunk_id,
            "bdc_id":              header["bdc_id"],
            "bdc_date":            str(header["bdc_date"]),
            "effective_date":      str(header["effective_date"]),
            "implementation_code": header["implementation_code"],
            "subject":             header["subject"],
            "section_id":          block["section_id"],
            "section_prefix":      section_prefix,
            "change_type":         block["change_type"],
            "amendment_text":      block["amendment_text"],
        }).execute()

    return len(blocks)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", default="bdc_updates")
    parser.add_argument("--fresh",   action="store_true",
                        help="Delete all existing BDC data before ingesting")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show extracted blocks without writing to DB")
    args = parser.parse_args()

    db  = get_db()
    col = args.collection

    pdf_dir = Path(config.RAW_PDFS_DIR)
    pdfs    = sorted(pdf_dir.glob("BDC*.pdf"))

    if not pdfs:
        print(f"No BDC*.pdf files found in {pdf_dir}")
        sys.exit(1)

    print(f"\n🔧 ingest_bdc.py")
    print(f"   collection={col!r}  fresh={args.fresh}  dry_run={args.dry_run}")
    print(f"   Found {len(pdfs)} BDC PDFs\n")

    if args.fresh and not args.dry_run:
        _delete_existing(db, col)

    total_blocks = 0
    for pdf_path in pdfs:
        total_blocks += ingest_bdc(pdf_path, db, col, args.dry_run)

    action = "Would insert" if args.dry_run else "Inserted"
    print(f"\n✅ Done — {action} {total_blocks} amendment blocks from {len(pdfs)} BDC PDFs.\n")


if __name__ == "__main__":
    main()
