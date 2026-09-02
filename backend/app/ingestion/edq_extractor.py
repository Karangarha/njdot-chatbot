"""EDQ line-item extraction from the DBE Goal Memo — vision-based.

The memo carries a "Capital Program Support Job Estimate Report by Job ID
and Category" table listing EDQ (Estimated Distribution Quantity) line
items. Unlike the memo's page-1 summary (``estimate_extractor``), this
table's page position varies per memo — it must be located, not assumed.

Two-step process, same scan-first-then-vision shape as the rest of
``app.ingestion``:
1. Locate which page(s) hold the table — a cheap text-layer regex scan when
   the PDF has a real text layer, else one multimodal call over low-DPI
   thumbnails of every page.
2. Render each located page at full DPI and transcribe its rows with one
   structured-output call per page.

Page-rendering helpers (``render_page_png``, ``_text_layer_hint_for_page``)
are reused from ``estimate_extractor`` rather than duplicated.
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.ingestion.estimate_extractor import (
    _text_layer_hint_for_page,
    pdf_page_count,
    render_page_png,
)
from app.observability import get_langfuse_handler, new_trace_id

logger = logging.getLogger(__name__)

_SECTION_KEYWORD_RE = re.compile(r"capital program support|job estimate report", re.IGNORECASE)
_MAX_LOCATE_PAGES = 30   # defensive cap; real memos run ~13 pages
_MAX_EXTRACT_PAGES = 6   # defensive cap on how many matched pages get transcribed
_LOCATE_DPI = 80         # thumbnail-quality, keeps the one multi-image locate call cheap


class EdqLineItem(BaseModel):
    job_id: Optional[str] = Field(None, description='The "Job ID" column value.')
    category: Optional[str] = Field(None, description='The "Category" column value.')
    item_description: Optional[str] = Field(
        None, description="The line item's description, as printed. Null if illegible.")
    estimated_quantity: Optional[str] = Field(None, description="As printed, e.g. '1,200'.")
    unit: Optional[str] = Field(None, description="Unit of measure, e.g. 'LF', 'TON', 'EA'.")
    # Always overwritten by extract_edq_items() with the known page number
    # after the call returns — not something the model needs to fill in.
    source_page: int = 0


class _EdqPageExtraction(BaseModel):
    """One page's worth of table rows — the per-page structured-output call."""

    section_title: Optional[str] = None
    items: List[EdqLineItem] = Field(default_factory=list)


class EdqExtraction(BaseModel):
    """Merged result across every page the section spans."""

    section_title: Optional[str] = None
    items: List[EdqLineItem] = Field(default_factory=list)
    pages_used: List[int] = Field(default_factory=list)


class _EdqSectionLocateResult(BaseModel):
    pages: List[int] = Field(
        default_factory=list,
        description="1-indexed page numbers containing the 'Capital Program "
                    "Support Job Estimate Report by Job ID and Category' table. "
                    "Include every page it continues onto. Empty if absent.",
    )


_LOCATE_SYSTEM_PROMPT = """\
You are shown thumbnail images of every page of an NJDOT DBE Goal memo, in \
order. Find the page(s) containing the "Capital Program Support Job Estimate \
Report by Job ID and Category" table -- a table of line items grouped by Job \
ID and Category, each with an estimated quantity and unit.

Return the 1-indexed page number(s) it appears on (it may continue across \
consecutive pages). Return an empty list if no such table is present.
"""

_EXTRACT_SYSTEM_PROMPT = """\
You transcribe one page of the "Capital Program Support Job Estimate Report \
by Job ID and Category" table from an NJDOT DBE Goal memo.

For each real line item row, extract: Job ID, Category, item description, \
estimated quantity, and unit. Skip section headers, column headers, and \
subtotal/total rows -- they are not line items.

Leave a field null rather than guess at illegible text. Do not invent rows.
"""


def locate_edq_section_pages(
    raw: bytes,
    llm: Any,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> List[int]:
    """One multimodal call over low-DPI thumbnails of every page (capped at
    ``_MAX_LOCATE_PAGES``), asking which page(s) hold the EDQ table.
    Returns ``[]`` if not found or on any failure."""
    page_count = pdf_page_count(raw)
    if page_count == 0:
        return []
    page_count = min(page_count, _MAX_LOCATE_PAGES)

    content: List[dict] = [{
        "type": "text",
        "text": "Pages 1 through %d, in order:" % page_count,
    }]
    for i in range(page_count):
        try:
            png = render_page_png(raw, i, dpi=_LOCATE_DPI)
        except Exception:
            logger.warning("locate_edq_section_pages: failed to render page %d", i + 1)
            continue
        b64 = base64.b64encode(png).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "low"},
        })

    try:
        structured_llm = llm.with_structured_output(_EdqSectionLocateResult)
        handler = get_langfuse_handler(trace_id=new_trace_id(seed=project_id)) if project_id else None
        invoke_config = {
            "callbacks": [handler] if handler else [],
            "run_name": "locate-edq-section",
            "metadata": {
                "langfuse_session_id": project_id,
                "langfuse_user_id": user_id,
                "langfuse_tags": ["edq_extraction"],
            },
        }
        result = structured_llm.invoke(
            [SystemMessage(content=_LOCATE_SYSTEM_PROMPT), HumanMessage(content=content)],
            config=invoke_config,
        )
        return sorted({p for p in result.pages if 1 <= p <= page_count})
    except Exception:
        logger.exception("locate_edq_section_pages: structured extraction failed")
        return []


def _extract_page(
    raw: bytes,
    page_index: int,
    llm: Any,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[_EdqPageExtraction]:
    try:
        png = render_page_png(raw, page_index)
    except Exception:
        logger.exception("_extract_page: failed to render page %d", page_index + 1)
        return None

    user_text = "This is page %d of an NJDOT DBE Goal memo's Job Estimate Report table." % (page_index + 1)
    hint = _text_layer_hint_for_page(raw, page_index)
    if hint:
        user_text += "\n\nEmbedded text layer for the same page, for reference:\n" + hint

    try:
        structured_llm = llm.with_structured_output(_EdqPageExtraction)
        handler = get_langfuse_handler(trace_id=new_trace_id(seed=project_id)) if project_id else None
        invoke_config = {
            "callbacks": [handler] if handler else [],
            "run_name": "extract-edq-page",
            "metadata": {
                "langfuse_session_id": project_id,
                "langfuse_user_id": user_id,
                "langfuse_tags": ["edq_extraction"],
            },
        }
        b64 = base64.b64encode(png).decode("ascii")
        return structured_llm.invoke(
            [
                SystemMessage(content=_EXTRACT_SYSTEM_PROMPT),
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
        logger.exception("_extract_page: structured extraction failed for page %d", page_index + 1)
        return None


def extract_edq_items(
    raw: bytes,
    llm: Any,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[EdqExtraction]:
    """Locate and transcribe the Job Estimate Report table. Never raises:
    returns ``None`` if the section can't be found or nothing extractable
    comes back, so the check resolves "Missing" rather than erroring."""
    page_count = pdf_page_count(raw)
    if page_count == 0:
        return None

    # Cheap pass first: a real text layer that matches the section keywords
    # needs no LLM call to locate.
    pages_1indexed: List[int] = []
    for i in range(min(page_count, _MAX_LOCATE_PAGES)):
        hint = _text_layer_hint_for_page(raw, i)
        if hint and _SECTION_KEYWORD_RE.search(hint):
            pages_1indexed.append(i + 1)

    if not pages_1indexed:
        pages_1indexed = locate_edq_section_pages(raw, llm, project_id=project_id, user_id=user_id)

    if not pages_1indexed:
        return None

    section_title: Optional[str] = None
    items: List[EdqLineItem] = []
    pages_used: List[int] = []
    for page_1indexed in pages_1indexed[:_MAX_EXTRACT_PAGES]:
        page_index = page_1indexed - 1
        page_result = _extract_page(raw, page_index, llm, project_id=project_id, user_id=user_id)
        if page_result is None:
            continue
        pages_used.append(page_1indexed)
        if section_title is None and page_result.section_title:
            section_title = page_result.section_title
        for item in page_result.items:
            item.source_page = page_1indexed
            items.append(item)

    if not items:
        return None

    return EdqExtraction(section_title=section_title, items=items, pages_used=pages_used)
