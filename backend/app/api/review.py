"""POST /api/review — Schedule compliance review endpoint.

Accepts two PDF uploads (schedule_pdf, narrative_pdf), converts them to
base64, and sends them to GPT-4o (primary) or claude-sonnet-4-20250514
(fallback) with a structured compliance-check prompt.  Returns a JSON
compliance report with a "model_used" field indicating which model ran.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime

import anthropic
import openai
from fastapi import APIRouter, File, HTTPException, UploadFile
from xerparser import Xer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["review"])

_OPENAI_MODEL = "gpt-4o"
_ANTHROPIC_MODEL = "claude-sonnet-4-20250514"

_SYSTEM_PROMPT = """\
You are an expert Construction Schedule Analyzer and Compliance Agent. Your primary objective is to evaluate a construction project schedule against a strict 38-point compliance checklist mandated by the Department of Transportation (DOT). You will process this data instantly to identify structural schedule errors and regulatory violations.

Data Context: The .XER File
You will be provided with schedule data that originated from a Primavera P6 .xer file. This file contains the Critical Path Method (CPM) data for the entire construction project. Because raw .xer files rely on complex internal database keys, the data has been pre-processed and normalized into a clean JSON array for your analysis.

Understanding the Data Structure
Every item in the JSON array represents a single schedule activity. You must evaluate these activities based on the following extracted fields:
* Activity Metadata: The unique activity_id and the human-readable activity_name.
* Timing & Duration: The start_date, finish_date, and duration_days.
* Logic & Float: The total_float (to identify delays or slippage), alongside predecessors and successors arrays to map the sequence of work.
* Constraints: Any hard constraints (e.g., "Must Finish On") that might override natural schedule logic.

WBS Hierarchy: A wbs_path array (Work Breakdown Structure). This provides the exact semantic context of where the task lives in the project phases (e.g., ["Milestones"] or ["Construction", "Pre-Stage 1A"]).

Your Core Evaluation Tasks
You will cross-reference this JSON data against the provided DOT Construction Manual rules to perform three types of checks:
1.  Schedule Quality & Logic: Flag activities with missing predecessors/successors, unjustified high/negative float, or improper constraints.
2.  Procedural Requirements: Verify that mobilization occurs after the Notice to Proceed (NTP) and that proper review windows (e.g., 14 or 21 days) are scheduled.
3.  Field & Seasonal Rules: Check specific construction phases against WBS paths to ensure compliance with seasonal weather constraints (e.g., winter paving bans)

Return ONLY a valid JSON object — no preamble, no markdown code fences, no explanation, date format must be (MM/DD/YYYY) including day of week.
The JSON must conform exactly to this schema:

{
  "project_name": "<string>",
  "project_duration_days": <number>,
  "summary": {
    "passed": <number>,
    "warnings": <number>,
    "failed": <number>,
    "manual_review": <number>
  },
  "checks": [
    {
      "id": "<string>",
      "category": "<string>",
      "name": "<string>",
      "status": "pass" | "warning" | "fail",
      "finding": "<string describing what was found>",
      "evidence": "<string quoting or citing specific data from the documents>"
    }
  ],
  "manual_review_items": [<string>, ...]
}

Rules:
- Every check listed below must appear in the "checks" array in the order given.
- status must be exactly "pass", "warning", or "fail" — nothing else.
- "finding" must be a clear sentence about whether the check passed or failed and why.
- "evidence" must quote or reference specific dates, durations, or text from the documents.
- If evidence is not present in the documents, note that explicitly.
- The "summary" counts must equal the actual counts of each status across all checks.
- The "manual_review_items" must be exactly the static list provided — do not modify it.

CHECKS TO RUN:

CATEGORY: Administrative Dates
- id: "schedule_duration", name: "Schedule Duration Under 3 Years"
- id: "ad_date_day", name: "Advertisement Date Falls on Tuesday or Thursday"
- id: "bid_date_day", name: "Bid Date Falls on Tuesday or Thursday"
- id: "ad_to_bid_gap", name: "15 Business Days: Advertisement to Bid"
- id: "bid_to_award_gap", name: "15 Business Days: Bid to Award"
- id: "award_to_construction", name: "Award to Construction Start Timeframe"

CATEGORY: Completion Milestones
- id: "substantial_to_final", name: "60 Calendar Days: Substantial to Final Completion"
- id: "substantial_before_oct1", name: "Substantial Completion Before October 1 (North of Rt. 195)"
- id: "no_completion_in_winter", name: "Completion Dates Not Between Dec 1 and Mar 15"

CATEGORY: Environmental & Permit Restrictions
- id: "inwater_window", name: "In-Water Work Within Apr 2 – Sep 14 Window"
- id: "cofferdam_window", name: "Cofferdam Activities Within Permitted Window"
- id: "wood_turtle_restriction", name: "Wood Turtle Restriction (Nov 1 – Apr 1) Respected"
- id: "row_availability", name: "ROW Availability Date Precedes Parcel Work"

CATEGORY: Winter Restrictions
- id: "no_concrete_winter", name: "No Concrete Activities Dec 15 – Mar 15"
- id: "no_paving_winter", name: "No Paving Activities Dec 15 – Mar 15"

CATEGORY: Working Drawings & Materials
- id: "working_drawing_review_time", name: "Working Drawing Review Durations (30/45 Days)"
- id: "box_beam_lead_time", name: "Concrete Box Beam Fabrication Lead Time (90+ Days)"
- id: "cure_time_present", name: "Cure Time Activities Present After Concrete Pours"

CATEGORY: Schedule Logic
- id: "negative_float", name: "No Negative Float Present"

CATEGORY: Narrative Completeness
- id: "narrative_production_rates", name: "Narrative: Anticipated Production Rates"
- id: "narrative_workforce", name: "Narrative: Anticipated Workforce"
- id: "narrative_winter_work", name: "Narrative: Winter Season Work Plan"
- id: "narrative_permits", name: "Narrative: Permit Requirements"
- id: "narrative_utilities", name: "Narrative: Utility Requirements"
- id: "narrative_row", name: "Narrative: ROW Requirements"
- id: "narrative_community", name: "Narrative: Community Commitments"
- id: "narrative_materials", name: "Narrative: Lead Time for Special Materials"
- id: "narrative_detours", name: "Narrative: Detours and Timeframe"
- id: "narrative_risks", name: "Narrative: Potential Risks / Anticipated Problems"
- id: "narrative_acceleration", name: "Narrative: Schedule Acceleration Description"
- id: "narrative_critical_milestones", name: "Narrative: Critical Milestones"
- id: "narrative_restricted_days", name: "Narrative: Restricted Working Days per Operation (Appendix B)"

MANUAL REVIEW ITEMS (include these verbatim in the "manual_review_items" array):
- "Utility alignment with Key Map and Special Provisions"
- "Gas/water/electric utility restriction windows (confirm with Special Provisions)"
- "Environmental permit compliance beyond narrative"
- "Landscape and planting restrictions"
- "EDQ items review"
- "Multi-year funding check (SP 108.10)"
- "Other nearby construction projects (105.06)"
- "ITS testing and burn-in period"
- "Summer shutdown restrictions for shore routes"
"""

_MANUAL_REVIEW_ITEMS = [
    "Utility alignment with Key Map and Special Provisions",
    "Gas/water/electric utility restriction windows (confirm with Special Provisions)",
    "Environmental permit compliance beyond narrative",
    "Landscape and planting restrictions",
    "EDQ items review",
    "Multi-year funding check (SP 108.10)",
    "Other nearby construction projects (105.06)",
    "ITS testing and burn-in period",
    "Summer shutdown restrictions for shore routes",
]

_USER_TEXT = (
    "Please perform the full compliance review on the two documents above "
    "and return the JSON report as specified."
)


def parse_xer_to_json(xer_text: str) -> list:
    try:
        xer = Xer(xer_text)
    except Exception as e:
        logger.error(f"Error parsing XER content: {e}")
        raise HTTPException(status_code=400, detail="Failed to parse XER file.")

    task_id_map = {}
    activities_data = {}

    def get_wbs_path(wbs_id):
        path = []
        if hasattr(xer, 'wbs_nodes'):
            current_wbs = xer.wbs_nodes.get(wbs_id)
            while current_wbs:
                wbs_name = getattr(current_wbs, 'wbs_name', getattr(current_wbs, 'name', ''))
                if wbs_name:
                    path.insert(0, wbs_name)
                parent_id = getattr(current_wbs, 'parent_wbs_id', None)
                current_wbs = xer.wbs_nodes.get(parent_id) if parent_id else None
        return path

    for activity in xer.tasks.values():
        internal_db_key = activity.uid  
        human_readable_id = getattr(activity, 'task_code', '') 
        task_id_map[internal_db_key] = human_readable_id
        
        wbs_id = getattr(activity, 'wbs_id', None)
        hierarchy_path = get_wbs_path(wbs_id) if wbs_id else []
        
        activities_data[human_readable_id] = {
            "activity_id": human_readable_id,
            "activity_name": getattr(activity, 'name', ''),
            "wbs_path": hierarchy_path,
            "start_date": activity.start.strftime('%Y-%m-%d') if getattr(activity, 'start', None) else None,
            "finish_date": activity.finish.strftime('%Y-%m-%d') if getattr(activity, 'finish', None) else None,
            "duration_days": int(getattr(activity, 'duration', 0) or 0),
            "total_float": int(getattr(activity, 'total_float', 0) or 0),
            "calendar_id": getattr(activity, 'clndr_id', ''),
            "predecessors": [],
            "successors": [],
            "constraints": {
                "type": getattr(activity, 'cstr_type', None) or "None",
                "date": activity.cstr_date.strftime('%Y-%m-%d') if getattr(activity, 'cstr_date', None) else None
            }
        }

    if hasattr(xer, 'relationships'):
        for relation in xer.relationships.values():
            pred_internal_key = getattr(relation, 'pred_task_id', None)
            succ_internal_key = getattr(relation, 'task_id', None)

            pred_readable_id = task_id_map.get(pred_internal_key)
            succ_readable_id = task_id_map.get(succ_internal_key)

            if pred_readable_id and pred_readable_id in activities_data:
                activities_data[pred_readable_id]["successors"].append(succ_readable_id)
                
            if succ_readable_id and succ_readable_id in activities_data:
                activities_data[succ_readable_id]["predecessors"].append(pred_readable_id)

    return list(activities_data.values())


def _parse_json(raw_text: str, model_label: str) -> dict:
    """Parse JSON from a model response, stripping markdown fences if needed."""
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        stripped = raw_text.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[-1]
            stripped = stripped.rsplit("```", 1)[0].strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            logger.error("%s returned non-JSON response: %s", model_label, raw_text[:500])
            raise HTTPException(
                status_code=500,
                detail=f"{model_label} did not return valid JSON. The response could not be parsed.",
            ) from exc


def _call_openai(schedule_json_str: str, narrative_b64: str) -> dict:
    """Send both documents to GPT-4o and return the parsed compliance report."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    client = openai.OpenAI(api_key=api_key)

    response = client.chat.completions.create(
        model=_OPENAI_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Here is the parsed schedule data in JSON format:\n{schedule_json_str}",
                    },
                    {
                        "type": "file",
                        "file": {
                            "filename": "narrative.pdf",
                            "file_data": f"data:application/pdf;base64,{narrative_b64}",
                        },
                    },
                    {"type": "text", "text": _USER_TEXT},
                ],
            },
        ],
    )

    raw_text = response.choices[0].message.content or ""
    return _parse_json(raw_text, "GPT-4o")


def _call_anthropic(schedule_json_str: str, narrative_b64: str) -> dict:
    """Send both documents to claude-sonnet-4-20250514 and return the parsed compliance report."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")

    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model=_ANTHROPIC_MODEL,
        max_tokens=8192,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Here is the parsed schedule data in JSON format:\n{schedule_json_str}",
                    },
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": narrative_b64,
                        },
                        "title": "Project Narrative",
                    },
                    {"type": "text", "text": _USER_TEXT},
                ],
            }
        ],
    )

    raw_text = message.content[0].text if message.content else ""
    return _parse_json(raw_text, "Claude")


@router.post(
    "/review",
    summary="Schedule compliance review",
    description=(
        "Accepts a CPM schedule XER file and a narrative PDF, sends both to GPT-4o "
        "(with Claude as fallback) for compliance analysis, and returns a "
        "structured JSON report with a 'model_used' field."
    ),
)
async def review_endpoint(
    schedule_file: UploadFile = File(..., description="CPM schedule XER file"),
    narrative_pdf: UploadFile = File(..., description="Project narrative PDF"),
) -> dict:
    """Run a schedule compliance review against NJDOT requirements."""
    # ── Read and encode files ──────────────────────────────────────────────────
    try:
        schedule_bytes = await schedule_file.read()
        narrative_bytes = await narrative_pdf.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read uploaded files: {exc}") from exc

    # Parse XER file to JSON
    xer_text = schedule_bytes.decode("utf-8", errors="ignore")
    schedule_json = parse_xer_to_json(xer_text)
    schedule_json_str = json.dumps(schedule_json)

    # Encode PDF
    narrative_b64 = base64.standard_b64encode(narrative_bytes).decode("utf-8")

    # ── Primary: GPT-4o ───────────────────────────────────────────────────────
    model_used = _OPENAI_MODEL
    try:
        result = _call_openai(schedule_json_str, narrative_b64)
        logger.info("Review completed via %s", _OPENAI_MODEL)
    except HTTPException:
        # JSON parse failure from OpenAI — propagate immediately, no fallback needed
        raise
    except Exception as openai_exc:
        logger.warning(
            "OpenAI call failed (%s: %s), falling back to %s",
            type(openai_exc).__name__,
            openai_exc,
            _ANTHROPIC_MODEL,
        )
        # ── Fallback: Claude ──────────────────────────────────────────────────
        model_used = _ANTHROPIC_MODEL
        try:
            result = _call_anthropic(schedule_json_str, narrative_b64)
            logger.info("Review completed via %s (fallback)", _ANTHROPIC_MODEL)
        except HTTPException:
            raise
        except Exception as anthropic_exc:
            logger.exception("Both OpenAI and Anthropic calls failed")
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Both AI providers failed. "
                    f"OpenAI: {type(openai_exc).__name__}: {openai_exc}. "
                    f"Anthropic: {type(anthropic_exc).__name__}: {anthropic_exc}."
                ),
            ) from anthropic_exc

    # ── Finalise response ─────────────────────────────────────────────────────
    result["model_used"] = model_used
    result["manual_review_items"] = _MANUAL_REVIEW_ITEMS

    return result
