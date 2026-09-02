"""Key map (key sheet) extraction — PyMuPDF text + one structured LLM call.

The key map is the cover sheet of an NJDOT plan set: it lists the utility
owners, the project mid-point latitude/longitude, the index of sheets, and
assorted project identifiers. Key sheets are CAD-generated vector PDFs, and
the two text extractors behave very differently on them: pdfplumber's
layout-aware ordering interleaves the decorative state-seal ring text through
the utility list ("ATLANTIC CITY ELECTRIC OFTHESTATE"), while PyMuPDF keeps
each block on its own lines — so this module reads **PyMuPDF text only**,
deliberately bypassing ``PDFParser``'s pdfplumber-first order.

Coordinates are parsed regex-first from the raw text (the printed values
survive extraction verbatim, e.g. ``LATTIUDE: 39�23'43"N`` — note the
sheet's own label typo and the mojibake degree sign); the LLM's values are
only a fallback, since LLMs botch DMS arithmetic.
"""

from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

import fitz  # PyMuPDF
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.compliance.geo import parse_coordinate
from app.observability import get_langfuse_handler, new_trace_id

logger = logging.getLogger(__name__)

# Below this much extractable text the sheet is treated as scanned/image-only
# (no OCR in scope) and extraction is skipped — dependent checks go "Missing".
_MIN_TEXT_CHARS = 80

# Key sheets run ~3.5k chars/page; cap the prompt defensively anyway.
_MAX_PROMPT_CHARS = 30_000

# Label-anchored coordinate lines. Loose on the label tail because the real
# Route 49 sheet misspells "LATITUDE" as "LATTIUDE"; the captured remainder of
# the line goes through geo.parse_coordinate, which handles DMS + mojibake.
_LAT_LINE_RE = re.compile(r"\bLAT+\w*\s*[:.]?\s*([^\n]{1,40})", re.IGNORECASE)
_LON_LINE_RE = re.compile(r"\bLONG\w*\s*[:.]?\s*([^\n]{1,40})", re.IGNORECASE)


class SheetIndexEntry(BaseModel):
    sheet_no: Optional[str] = Field(None, description='Sheet number or range, e.g. "1" or "44-59"')
    title: str = Field(description='Sheet description, e.g. "TYPICAL SECTIONS"')


class KeyMapExtraction(BaseModel):
    """Structured contents of a key map sheet. Everything is optional —
    extract only what the sheet actually shows."""

    utility_owners: List[str] = Field(
        default_factory=list,
        description="Utility companies/departments listed under the UTILITIES "
                    'heading, e.g. "ATLANTIC CITY ELECTRIC", "VERIZON NJ".',
    )
    latitude_raw: Optional[str] = Field(
        None, description='Latitude exactly as printed, e.g. 39°23\'43"N (do not convert).')
    longitude_raw: Optional[str] = Field(
        None, description='Longitude exactly as printed, e.g. 75°02\'25"W (do not convert).')
    latitude_decimal: Optional[float] = Field(
        None, description="Latitude converted to decimal degrees, if confident.")
    longitude_decimal: Optional[float] = Field(
        None, description="Longitude in decimal degrees (negative for degrees West), if confident.")
    route: Optional[str] = Field(None, description='Route designation, e.g. "NJ Route 49".')
    municipalities: List[str] = Field(default_factory=list)
    counties: List[str] = Field(default_factory=list)
    contract_no: Optional[str] = Field(None, description="NJDOT contract number.")
    federal_project_no: Optional[str] = Field(None, description="Federal project number.")
    structure_nos: List[str] = Field(
        default_factory=list, description='Structure numbers in the project limits, e.g. "0605-151".')
    mileposts: Optional[str] = Field(None, description='Milepost range, e.g. "M.P. 36.17 to 36.31".')
    project_length: Optional[str] = Field(None, description='Total project length as printed.')
    sheet_index: List[SheetIndexEntry] = Field(
        default_factory=list, description="The INDEX OF SHEETS table, in listed order.")
    misc_notes: List[str] = Field(
        default_factory=list,
        description="Other substantive project notes (design traffic data, governing "
                    "specifications, applicable detail booklets...). Not boilerplate.",
    )


_EXTRACTION_SYSTEM_PROMPT = """\
You extract structured data from the text of an NJDOT key map ("key sheet") — \
the cover sheet of a roadway/bridge plan set.

The text comes from a CAD-generated PDF, so reading order is jumbled and some \
content is decorative noise you must ignore: the circular state-seal text \
(often one or two letters per line, spelling "LIBERTY AND PROSPERITY" / "THE \
GREAT SEAL OF THE STATE OF NEW JERSEY"), CAD file paths, pen tables, scale \
callouts, and signature blocks.

Guidance:
- utility_owners: the companies/departments under the "UTILITIES" heading. \
Report each name cleanly, without any interleaved noise words.
- latitude_raw / longitude_raw: copy the printed coordinate strings verbatim \
(labels may be misspelled, e.g. "LATTIUDE"; the degree symbol may appear as a \
garbled character — copy the digits and N/S/E/W letter as printed).
- sheet_index: the "INDEX OF SHEETS" table. Extraction order may scramble the \
pairing between sheet numbers and descriptions — reconstruct the pairing \
best-effort, keeping descriptions in their listed order.
- Only report what is present in the text; leave everything else null/empty.
"""


def extract_keymap_pages(raw: bytes) -> List[dict]:
    """Per-page PyMuPDF text in ``PDFParser.extract_text()``'s page shape,
    so the output drops straight into ``chunk_special_provision``. PyMuPDF
    only — see the module docstring for why not pdfplumber."""
    doc = fitz.open(stream=raw, filetype="pdf")
    try:
        return [
            {
                "page_num": i,
                "text": page.get_text() or "",
                "char_count": len(page.get_text() or ""),
                "extractor": "pymupdf",
            }
            for i, page in enumerate(doc, start=1)
        ]
    finally:
        doc.close()


def extract_keymap_text(raw: bytes) -> str:
    """All-pages text via PyMuPDF only (see module docstring for why not
    ``PDFParser``'s pdfplumber-first order)."""
    return "\n\n".join(p["text"] for p in extract_keymap_pages(raw))


def _regex_coordinate(text: str, *, longitude: bool) -> Optional[str]:
    """First label-anchored coordinate line whose value parses into a
    plausible range (lat 30–50°N, lon 60–85°W) — the range filter skips
    lookalike labels ("LATERAL ...") without giving up the downstream
    NJ-bounds ``suspect`` handling for genuinely bad LLM-supplied values."""
    pattern = _LON_LINE_RE if longitude else _LAT_LINE_RE
    for match in pattern.finditer(text):
        raw = match.group(1).strip()
        value = parse_coordinate(raw, is_longitude=longitude)
        if value is None:
            continue
        if longitude and -85.0 <= value <= -60.0:
            return raw
        if not longitude and 30.0 <= value <= 50.0:
            return raw
    return None


def extract_key_map(
    raw: bytes,
    llm: Any,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[KeyMapExtraction]:
    """One structured-output LLM call over the key map's PyMuPDF text.

    Regex-found coordinate strings override the LLM's (regex reads the
    printed value directly). Never raises: returns ``None`` when the sheet
    has no extractable text or the LLM call fails with no regex coordinates
    to fall back on — the review proceeds and keymap checks go "Missing".
    """
    try:
        text = extract_keymap_text(raw)
    except Exception:
        logger.exception("extract_key_map: PyMuPDF text extraction failed")
        return None
    if len(text.strip()) < _MIN_TEXT_CHARS:
        logger.info(
            "extract_key_map: only %d chars of text extracted — likely a scanned/"
            "image-only sheet; skipping extraction", len(text.strip()),
        )
        return None

    lat_raw = _regex_coordinate(text, longitude=False)
    lon_raw = _regex_coordinate(text, longitude=True)

    extraction: Optional[KeyMapExtraction] = None
    try:
        structured_llm = llm.with_structured_output(KeyMapExtraction)
        handler = get_langfuse_handler(trace_id=new_trace_id(seed=project_id)) if project_id else None
        invoke_config = {
            "callbacks": [handler] if handler else [],
            "run_name": "extract-key-map",
            "metadata": {
                "langfuse_session_id": project_id,
                "langfuse_user_id": user_id,
                "langfuse_tags": ["key_map_extraction"],
            },
        }
        extraction = structured_llm.invoke(
            [
                SystemMessage(content=_EXTRACTION_SYSTEM_PROMPT),
                HumanMessage(content=text[:_MAX_PROMPT_CHARS]),
            ],
            config=invoke_config,
        )
    except Exception:
        logger.exception("extract_key_map: structured LLM extraction failed")

    if extraction is None:
        if not (lat_raw or lon_raw):
            return None
        # LLM failed but the coordinates parsed — the deterministic geo check
        # can still run on a coordinates-only extraction.
        extraction = KeyMapExtraction()
    if lat_raw:
        extraction.latitude_raw = lat_raw
    if lon_raw:
        extraction.longitude_raw = lon_raw
    return extraction


def render_keymap_facts(extraction: KeyMapExtraction, geo: Optional[Any]) -> str:
    """Render the extraction (+ the deterministic ``RegionResult``) as the
    stable "KEY MAP FACTS" evidence block shared by every keymap-sourced
    check. The ``GEOGRAPHY (deterministic)`` line is what
    ``substantial_regional_deadlines`` reads."""
    lines: List[str] = ["KEY MAP FACTS (extracted from the project key map sheet):"]

    if extraction.utility_owners:
        lines.append(
            "Utility owners/departments listed on the key map: "
            + "; ".join(extraction.utility_owners)
        )
    else:
        lines.append("Utility owners/departments listed on the key map: none found.")

    coords = []
    if extraction.latitude_raw:
        coords.append(f"latitude {extraction.latitude_raw}")
    if extraction.longitude_raw:
        coords.append(f"longitude {extraction.longitude_raw}")
    if coords:
        lines.append("Project coordinates (as printed): " + ", ".join(coords))

    if geo is not None:
        if geo.region:
            lines.append(f"GEOGRAPHY (deterministic): {geo.region} of I-195 — {geo.detail}")
        else:
            lines.append(f"GEOGRAPHY (deterministic): unresolved — {geo.detail}")

    ids = []
    if extraction.route:
        ids.append(f"Route: {extraction.route}")
    if extraction.counties:
        ids.append("Counties: " + ", ".join(extraction.counties))
    if extraction.municipalities:
        ids.append("Municipalities: " + ", ".join(extraction.municipalities))
    if ids:
        lines.append(" | ".join(ids))

    ids2 = []
    if extraction.contract_no:
        ids2.append(f"Contract No.: {extraction.contract_no}")
    if extraction.federal_project_no:
        ids2.append(f"Federal Project No.: {extraction.federal_project_no}")
    if extraction.structure_nos:
        ids2.append("Structures: " + ", ".join(extraction.structure_nos))
    if extraction.mileposts:
        ids2.append(f"Mileposts: {extraction.mileposts}")
    if extraction.project_length:
        ids2.append(f"Project length: {extraction.project_length}")
    if ids2:
        lines.append(" | ".join(ids2))

    if extraction.sheet_index:
        entries = "; ".join(
            f"{e.sheet_no}: {e.title}" if e.sheet_no else e.title
            for e in extraction.sheet_index
        )
        lines.append(f"Sheet index: {entries}")

    if extraction.misc_notes:
        lines.append("Notes: " + " / ".join(extraction.misc_notes))

    return "\n".join(lines)
