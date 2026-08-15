"""Engineer's Estimate extraction from the DBE Goal Memo — vision-based.

NJDOT's DBE/ESBE Goal request memo (form ``SI01``) carries the Final Design
Engineer's Estimate on its first page, which is what the
``substantial_to_final`` completion-gap rule keys off (60 days at or below
$50M, 90 days above).

**These memos are scans.** The real Route 49 memo has *zero* extractable text
on all 13 pages — pdfplumber and PyMuPDF both return nothing — so unlike the
key map (``keymap_extractor``, which reads a real text layer) there is no
text to parse and no regex cross-check available. Page 1 is rendered to a PNG
and transcribed by a multimodal model instead. When a text layer *does* exist
(a digitally-generated estimate), it is passed alongside the image as a hint,
so one code path serves both.

The model only transcribes; ``app.compliance.cost.parse_money`` does the
arithmetic on the verbatim string it returns.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, List, Optional

import fitz  # PyMuPDF
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.observability import get_langfuse_handler, new_trace_id

logger = logging.getLogger(__name__)

_DEFAULT_DPI = 200
# Fallback ladder if the rendered page is too large to send comfortably.
# 170 dpi (~1445x1870) is already comfortably legible on a real memo.
_DPI_LADDER = (200, 150, 110)
_MAX_PNG_BYTES = 4_000_000
_MAX_EDGE_PX = 2200

# A text layer shorter than this is treated as absent (scanned page).
_MIN_TEXT_HINT_CHARS = 40
_MAX_TEXT_HINT_CHARS = 4_000


class LabeledAmount(BaseModel):
    label: str = Field(description="The printed label this amount appears under.")
    amount_raw: str = Field(description="The amount exactly as printed, including $ and commas.")


class EstimateExtraction(BaseModel):
    """Structured contents of page 1 of an NJDOT DBE/ESBE Goal memo."""

    engineers_estimate_raw: Optional[str] = Field(
        None,
        description="The Engineer's Estimate exactly as printed, including the dollar "
                    "sign, comma separators and cents — e.g. \"$14,973,645.25\". "
                    "Null if the page has no Engineer's Estimate line.",
    )
    engineers_estimate_usd: Optional[float] = Field(
        None, description="The same amount as a plain number, if you are confident. Fallback only.")
    cost_basis_label: Optional[str] = Field(
        None, description="The printed label the amount came from, e.g. \"Engineer's Estimate\".")
    other_amounts: List[LabeledAmount] = Field(
        default_factory=list,
        description="Any OTHER currency amounts printed on the page. Currency only — "
                    "never percentages, phone numbers, project/job numbers or dates.",
    )
    project_name: Optional[str] = None
    municipality_county: Optional[str] = None
    federal_project_number: Optional[str] = None
    njdot_job_number: Optional[str] = None
    memo_date: Optional[str] = Field(None, description="The memo's Date field, as printed.")
    dbe_goal_percent: Optional[str] = Field(
        None, description="The DBE GOAL percentage if filled in (often handwritten).")
    form_id: Optional[str] = Field(
        None, description="Form identifier from the footer, e.g. \"SI01 - DBE/ESBE Goal - Jan 2023\".")
    page_transcription: str = Field(
        default="",
        description="A faithful markdown transcription of the page's readable content. "
                    "This is what Document Q&A searches, so include the field labels.",
    )
    notes: List[str] = Field(default_factory=list)


_EXTRACTION_SYSTEM_PROMPT = """\
You transcribe page 1 of an NJDOT DBE/ESBE Goal request memo (form SI01). The \
page is a scan of a printed form that also carries handwritten annotations.

Your most important job is the Engineer's Estimate:
- Find the line labeled "Engineer's Estimate" and copy the amount VERBATIM — \
dollar sign, comma separators and cents exactly as printed. Do not round, \
reformat, or recompute it.
- Only a CURRENCY amount can be the Engineer's Estimate. Percentages (e.g. \
"DBE GOAL: 12.0 %"), phone numbers, Federal Project Numbers, NJDOT Job \
Numbers, dates, and day counts (e.g. "5 calendar days") are NEVER the \
estimate, and must never appear in other_amounts either.
- Parts of this form are filled in by hand (the DBE/ESBE goal, signatures, \
received dates). The Engineer's Estimate is printed — never substitute a \
handwritten number for it.
- If the page has no "Engineer's Estimate" label, leave engineers_estimate_raw \
null rather than guessing from some other figure, and record what you did see \
in other_amounts.

General rules:
- Transcribe only what is legible. Use "[illegible]" for characters you cannot \
read — never invent or infer a digit.
- Leave any field not present on the page null or empty.
"""


def render_page_png(raw: bytes, page_index: int = 0, dpi: int = _DEFAULT_DPI) -> bytes:
    """Render one 0-indexed page to PNG bytes, stepping the DPI down if the
    result is too large to send comfortably."""
    doc = fitz.open(stream=raw, filetype="pdf")
    try:
        if doc.page_count == 0:
            raise ValueError("PDF has no pages")
        if page_index >= doc.page_count:
            raise ValueError(f"page_index {page_index} out of range ({doc.page_count} pages)")
        page = doc[page_index]
        ladder = (dpi,) + tuple(d for d in _DPI_LADDER if d < dpi)
        png = b""
        for step in ladder:
            pix = page.get_pixmap(dpi=step, colorspace=fitz.csRGB, alpha=False)
            png = pix.tobytes("png")
            if len(png) <= _MAX_PNG_BYTES and max(pix.width, pix.height) <= _MAX_EDGE_PX:
                return png
            logger.info(
                "render_page_png: page %d, %d dpi gave %dx%d (%d bytes) — stepping down",
                page_index, step, pix.width, pix.height, len(png),
            )
        return png  # last (smallest) attempt
    finally:
        doc.close()


def render_page1_png(raw: bytes, dpi: int = _DEFAULT_DPI) -> bytes:
    """Render page 1 to PNG bytes, stepping the DPI down if the result is too
    large to send comfortably."""
    return render_page_png(raw, 0, dpi)


def pdf_page_count(raw: bytes) -> int:
    """Number of pages in the PDF. 0 on any failure to open it."""
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception:
        return 0
    try:
        return doc.page_count
    finally:
        doc.close()


def _text_layer_hint_for_page(raw: bytes, page_index: int) -> Optional[str]:
    """One page's text layer, when the PDF has one. Scanned memos return None."""
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception:
        return None
    try:
        if page_index >= doc.page_count:
            return None
        text = (doc[page_index].get_text() or "").strip()
    finally:
        doc.close()
    if len(text) < _MIN_TEXT_HINT_CHARS:
        return None
    return text[:_MAX_TEXT_HINT_CHARS]


def _text_layer_hint(raw: bytes) -> Optional[str]:
    """Page 1's text layer, when the PDF has one. Scanned memos return None."""
    return _text_layer_hint_for_page(raw, 0)


def extract_estimate(
    raw: bytes,
    llm: Any,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[EstimateExtraction]:
    """One multimodal structured-output call over page 1 of the estimate PDF.

    Never raises: returns ``None`` if the page can't be rendered or the model
    call fails, so the review proceeds and the gap check reports "Missing".
    """
    try:
        png = render_page1_png(raw)
    except Exception:
        logger.exception("extract_estimate: failed to render page 1")
        return None

    user_text = (
        "This is page 1 of an NJDOT DBE/ESBE Goal memo. Extract the Engineer's "
        "Estimate and the project identifiers, and transcribe the page."
    )
    hint = _text_layer_hint(raw)
    if hint:
        user_text += (
            "\n\nThe PDF also carries this embedded text layer for the same page — "
            "use it to confirm exact digits, but the image is authoritative:\n" + hint
        )

    try:
        structured_llm = llm.with_structured_output(EstimateExtraction)
        handler = get_langfuse_handler(trace_id=new_trace_id(seed=project_id)) if project_id else None
        invoke_config = {
            "callbacks": [handler] if handler else [],
            "run_name": "extract-estimate",
            "metadata": {
                "langfuse_session_id": project_id,
                "langfuse_user_id": user_id,
                "langfuse_tags": ["estimate_extraction"],
            },
        }
        b64 = base64.b64encode(png).decode("ascii")
        return structured_llm.invoke(
            [
                SystemMessage(content=_EXTRACTION_SYSTEM_PROMPT),
                HumanMessage(content=[
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
                    },
                ]),
            ],
            config=invoke_config,
        )
    except Exception:
        logger.exception("extract_estimate: structured vision extraction failed")
        return None


def render_estimate_facts(extraction: EstimateExtraction, cost_gap: Optional[Any]) -> str:
    """Stable "PROJECT COST FACTS" block — the shared evidence string for
    every check sourcing ``"estimate"``, and the text indexed for chat."""
    lines: List[str] = ["PROJECT COST FACTS (from the project's DBE Goal Memo / Engineer's Estimate):"]

    if extraction.engineers_estimate_raw:
        label = extraction.cost_basis_label or "Engineer's Estimate"
        lines.append(f"{label} (as printed): {extraction.engineers_estimate_raw}")
    else:
        lines.append("Engineer's Estimate: not found on the page.")

    if cost_gap is not None:
        lines.append(f"COST GAP (deterministic): {cost_gap.detail}")

    ids = []
    if extraction.project_name:
        ids.append(f"Project: {extraction.project_name}")
    if extraction.municipality_county:
        ids.append(f"Municipality/County: {extraction.municipality_county}")
    if extraction.federal_project_number:
        ids.append(f"Federal Project No.: {extraction.federal_project_number}")
    if extraction.njdot_job_number:
        ids.append(f"NJDOT Job No.: {extraction.njdot_job_number}")
    if extraction.memo_date:
        ids.append(f"Memo date: {extraction.memo_date}")
    if extraction.dbe_goal_percent:
        # The model may or may not include the "%" (the form prints it
        # separately from the handwritten value) — normalize either way.
        ids.append(f"DBE goal: {extraction.dbe_goal_percent.strip().rstrip('%').strip()}%")
    if ids:
        lines.append(" | ".join(ids))

    if extraction.other_amounts:
        lines.append(
            "Other amounts on the page: "
            + "; ".join(f"{a.label}: {a.amount_raw}" for a in extraction.other_amounts)
        )

    if extraction.notes:
        lines.append("Notes: " + " / ".join(extraction.notes))

    return "\n".join(lines)
