"""Utility Agreement Plan extraction — vision-based.

Utility Agreement Plan sheets (one per utility company: gas, water/sewer,
telecom, electric, etc.) are CAD-exported engineering drawings, not text
documents. Confirmed on 3 real Route 49 sheets: pdfplumber/PyMuPDF text
extraction returns every drawing label in CAD-draw order (not reading
order) -- unusable, sometimes character-reversed. Only a vision call over
the rendered page reliably reads the title block and the lane-closure
schedule table. Same shape as ``estimate_extractor`` (also a scan-like
document); ``render_page1_png``/``_text_layer_hint`` are reused from there
rather than duplicated.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.ingestion.estimate_extractor import render_page1_png, _text_layer_hint
from app.observability import get_langfuse_handler, new_trace_id

logger = logging.getLogger(__name__)


class UtilityAgreementExtraction(BaseModel):
    """Structured contents of page 1 of a Utility Agreement Plan sheet."""

    drawing_id: Optional[str] = Field(None, description='Sheet/drawing number, e.g. "UA-01".')
    route: Optional[str] = Field(None, description='Route number/name, e.g. "Route 49".')
    mp_range: Optional[str] = Field(None, description='Milepost range, e.g. "MP 36.17 TO MP 36.31".')
    contract_number: Optional[str] = None
    county: Optional[str] = None
    municipality: Optional[str] = None
    utility_owner: Optional[str] = Field(
        None, description='The utility company/agency named on the sheet, e.g. "South Jersey Gas Company".')
    utility_type: Optional[str] = Field(
        None, description="What kind of utility: gas, water, sewer, electric, telecom, etc.")
    lane_closure_schedule: Optional[str] = Field(
        None, description="The allowable lane closure hour schedule text, if present, verbatim.")
    notes: List[str] = Field(default_factory=list, description="Any other legible notes worth keeping.")


_EXTRACTION_SYSTEM_PROMPT = """\
You read page 1 of an NJDOT Utility Agreement Plan sheet -- a CAD-exported \
engineering drawing showing a utility company's facilities along a road \
project, with a title block and often a traffic-control/lane-closure notes \
box.

The drawing body (pole/manhole callouts, utility line routing) is dense and \
mostly not useful to transcribe. Focus on:
1. The title block (usually bottom-right or a corner box): route, milepost \
   range, contract number, county, municipality, utility owner/company name, \
   drawing/sheet ID.
2. Any lane-closure / allowable-hours traffic control schedule text, if \
   present, copied verbatim.

Leave any field not present null. Do not guess at illegible text.
"""


def extract_utility_plan(
    raw: bytes,
    llm: Any,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[UtilityAgreementExtraction]:
    """One multimodal structured-output call over page 1 of a Utility
    Agreement Plan sheet. Never raises: returns ``None`` on any failure."""
    try:
        png = render_page1_png(raw)
    except Exception:
        logger.exception("extract_utility_plan: failed to render page 1")
        return None

    user_text = (
        "This is page 1 of an NJDOT Utility Agreement Plan sheet. Extract the "
        "title block facts and any lane-closure schedule."
    )
    hint = _text_layer_hint(raw)
    if hint:
        user_text += (
            "\n\nEmbedded (unreliable, CAD-scrambled) text layer for reference "
            "only:\n" + hint[:1000]
        )

    try:
        structured_llm = llm.with_structured_output(UtilityAgreementExtraction)
        handler = get_langfuse_handler(trace_id=new_trace_id(seed=project_id)) if project_id else None
        invoke_config = {
            "callbacks": [handler] if handler else [],
            "run_name": "extract-utility-plan",
            "metadata": {
                "langfuse_session_id": project_id,
                "langfuse_user_id": user_id,
                "langfuse_tags": ["utility_plan_extraction"],
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
        logger.exception("extract_utility_plan: structured vision extraction failed")
        return None


def render_utility_plan_facts(extraction: UtilityAgreementExtraction) -> str:
    """Searchable text block -- what gets embedded/indexed for Document Q&A."""
    lines: List[str] = ["UTILITY AGREEMENT PLAN"]

    ids = []
    if extraction.utility_owner:
        ids.append(f"Utility owner: {extraction.utility_owner}")
    if extraction.utility_type:
        ids.append(f"Utility type: {extraction.utility_type}")
    if extraction.drawing_id:
        ids.append(f"Drawing: {extraction.drawing_id}")
    if extraction.route:
        ids.append(f"Route: {extraction.route}")
    if extraction.mp_range:
        ids.append(f"MP range: {extraction.mp_range}")
    if extraction.contract_number:
        ids.append(f"Contract No.: {extraction.contract_number}")
    if extraction.county:
        ids.append(f"County: {extraction.county}")
    if extraction.municipality:
        ids.append(f"Municipality: {extraction.municipality}")
    if ids:
        lines.append(" | ".join(ids))

    if extraction.lane_closure_schedule:
        lines.append("Lane closure schedule:\n" + extraction.lane_closure_schedule)

    if extraction.notes:
        lines.append("Notes: " + " / ".join(extraction.notes))

    return "\n".join(lines)
