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

from app.ingestion.session_chunker import xer_to_markdown

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["review"])

_OPENAI_MODEL = "gpt-4o"
_ANTHROPIC_MODEL = "claude-sonnet-4-20250514"


_SYSTEM_PROMPT = """\
You are an expert Construction Schedule Compliance Agent. Your objective is to evaluate Critical Path Method (CPM) project schedules against Department of Transportation (DOT) compliance rules.

### 1. DATA CONTEXT: THE SCHEDULE MARKDOWN
You are evaluating a Markdown document derived from a Primavera P6 (.xer) file. It is organized into these sections:

**## Calendar** — project calendar name, working days (e.g., Mon–Fri), and holiday exception dates. Use this for all working-day and business-day calculations. If absent, assume a standard Mon–Fri calendar with US federal holidays.

**## Milestones** — table of zero-duration activities (ID | Name | Date | Float | Predecessors). Includes key dates: Advertisement (M100), Bid Opening, Award, Construction Start, Substantial Completion, Contract Completion.

**## Activities with Negative Float** — all activities where float < 0, sorted ascending. Includes WBS phase path.

**## Activities with Mandatory Constraints** — activities with date constraints applied (constraint type and date shown).

**## Phase: [WBS Path]** — one section per WBS phase, containing a table of all activities: ID | Name | Start | Finish | Duration (days) | Float | Predecessors.

Use activity IDs and names verbatim in your evidence. Every activity in the schedule appears in exactly one Phase section.

### 2. EXECUTION CONSTRAINTS & WORKFLOW
For EVERY rule specified under "CHECKS TO RUN", you must perform a strict sequential analysis in your internal scratchpad:
1. **Locate Data:** Identify the relevant activities using names, types, or `wbs_path`.
2. **Execute Reasoning:** Compare the data fields directly against the target constraint values. If checking a day of the week, inspect the day provided in the date string.
3. **Determine Status:** Set to "pass", "fail", or "warning" based on objective comparison. If data required for a geographic or regional check is completely absent from the schedule, select "warning".
4. **Extract Evidence:** Quote explicit identifiers, dates, paths, or values used to determine the result.

### 3. OUTPUT SCHEMA
Return ONLY a valid JSON object. Do not include markdown code fences. 

{
  "project_name": "<string or 'Unknown'>",
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
      "reasoning": "<Step-by-step logic detailing how the schedule data matches or violates the specific rule parameters>",
      "status": "pass" | "warning" | "fail",
      "finding": "<A single clear sentence explaining why the check passed, failed, or generated a warning>",
      "evidence": "<Direct quote or citation of relevant activity IDs, dates, durations, or paths used>"
    }
  ],
  "manual_review_items": [<string>, ...]
}

### 4. RIGID EVALUATION RULES
- **Completeness:** Every single item under "CHECKS TO RUN" must exist in the output `checks` array in the exact order listed.
- **Strict Status Enums:** The `status` field must be exactly "pass", "warning", or "fail". No deviations.
- **Weekday Verifications:** For `ad_date_day` and `bid_date_day`, if the explicit day string says "Tuesday" or "Thursday", the status is "pass". Other weekdays are "fail".
- **Duration Calculation:** For `schedule_duration`, locate the start_date of M100 and end_date of M950. Calculate the duration. Do NOT include weekends or holidays. It must be under 3 years.
- **Award to Construction:** For `award_to_construction`, determine project type. Timeframes must strictly be: 40 business days (State), 55 business days (Federal), or 25-55 business days (Pavement Preservation). Mark "warning" if project type is unknown.
- **Substantial to Final Gap:** For `substantial_to_final`, determine the project budget. Ensure a 60-calendar-day gap for projects $50M or less, and a 90-day gap for projects over $50M. Mark "warning" if budget is unknown.
- **Regional Completion:** For `substantial_regional_deadlines`, check geography. Substantial completion must be before October 1 for North of Route 195, and October 15 for South of Route 195. Mark "warning" if geography is unknown.
- **Missing Data Fallback:** If a checklist rule cannot be evaluated due to missing context, mark status as "warning", state "Data unavailable for verification" in the finding, and "No data found" in evidence.
- **Summary Congruence:** Summary counts must exactly equal the mathematical sum of the statuses in the `checks` array.

### 5. CHECKS TO RUN

CATEGORY: Administrative Dates
- id: "schedule_duration", name: "Schedule Duration Under 3 Years (Exclude Weekends and Holidays)"
- id: "ad_date_day", name: "Advertisement Date Falls on Tuesday or Thursday"
- id: "bid_date_day", name: "Bid Date Falls on Tuesday or Thursday"
- id: "ad_to_bid_gap", name: "15 Business Days: Advertisement to Bid"
- id: "bid_to_award_gap", name: "15 Business Days: Bid to Award"
- id: "award_to_construction", name: "Award to Construction Start Timeframe (40 days State / 55 days Federal / 25-55 days Pavement)"

CATEGORY: Completion Milestones
- id: "substantial_to_final", name: "Substantial to Final Completion Gap (60 Days <= $50M / 90 Days > $50M)"
- id: "substantial_regional_deadlines", name: "Substantial Completion Before Oct 1 (North 195) or Oct 15 (South 195)"
- id: "no_completion_in_winter", name: "Completion Dates Not Between Dec 15 and Mar 15"

CATEGORY: Environmental, Landscape & Utilities
- id: "row_availability", name: "ROW Availability Date Precedes Parcel Work"
- id: "landscape_season", name: "Landscape/Planting Limited to Mar 1-May 15 or Aug 15-Dec 1"
- id: "gas_interruption", name: "No Gas Service Interruptions Oct 1 to Apr 1"
- id: "water_interruption", name: "No Water Service Interruptions Apr 1 to Sep 30"
- id: "electric_interruption", name: "No Electric Service Interruptions Jun 1 to Sep 30"

CATEGORY: Winter Restrictions
- id: "no_concrete_winter", name: "No Concrete Activities Dec 15 – Mar 15"
- id: "no_paving_winter", name: "No Paving Activities Dec 15 – Mar 15"

CATEGORY: Working Drawings, Materials & ITS
- id: "working_drawing_review_time", name: "Working Drawing Review Durations (30/45 Days)"
- id: "steel_pole_lead_time", name: "Steel Traffic Signal Pole Fabrication Lead Time (4 Months)"
- id: "aluminum_pole_lead_time", name: "Aluminum Lighting/Signal Pole Fabrication Lead Time (2 Months)"
- id: "controller_lead_time", name: "Traffic Signal Controller Fabrication Lead Time (4 Months)"
- id: "its_burn_in", name: "ITS New Installation Observation Period (6 Months)"

CATEGORY: Schedule Logic
- id: "no_negative_float", name: "No Negative Float Present"
- id: "no_lag", name: "No Lag Present"
- id: "no_open_ends", name: "No Open Ends Present"
- id: "no_mandatory_constraints", name: "No Mandatory Constraints Applied"

CATEGORY: Manual Review
For each item below, evaluate what you can from the schedule JSON and the narrative PDF. Use "pass" if there is clear evidence in the documents that the requirement is met, "fail" if there is clear evidence of non-compliance, or "warning" if data is insufficient to make a determination.
- id: "utility_alignment", name: "Utility Alignments Match Key Sheet and Special Provisions"
- id: "environmental_permit", name: "Environmental Permit Compliance Beyond Narrative"
- id: "edq_items", name: "EDQ Items Cross-Referenced for Missing Construction Activities"
- id: "multi_year_funding", name: "Multi-Year Funding Logic Applied (SP 108.10)"
- id: "nearby_projects", name: "No Conflicts with Nearby Construction Projects (105.06)"
- id: "traffic_control_staging", name: "Traffic Control Staging Sequences Match Schedule Narrative"
- id: "summer_shutdown", name: "Summer Shutdown Restrictions Applied (NJ Shore Routes)"
"""

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


def parse_xer_calendars(xer_text: str) -> list:
    """Extract calendar definitions from raw XER text by parsing the CALENDAR table directly.

    xerparser exposes clndr_data as a scalar float (day_hr_cnt) rather than the
    nested parenthetical string we need, so we bypass it and read the raw rows.

    clndr_data encodes working days in a DaysOfWeek section where day numbers
    1–7 map to Sun–Sat.  A day is working when its entry contains at least one
    work-period marker (s|HH:MM), otherwise it is empty (non-working).
    Exception dates are Primavera serials (days since Dec 30, 1899).
    """
    from datetime import date as _date, timedelta as _td
    import re as _re

    _P6_EPOCH = _date(1899, 12, 30)
    _ABBREV   = {1: "Sun", 2: "Mon", 3: "Tue", 4: "Wed", 5: "Thu", 6: "Fri", 7: "Sat"}

    # ── Parse CALENDAR table rows from raw XER text ───────────────────────────
    in_calendar   = False
    clndr_id_idx  = clndr_name_idx = clndr_data_idx = -1
    raw_calendars: dict = {}   # clndr_id (str) → {name, clndr_data}

    for line in xer_text.splitlines():
        if line.startswith("%T\t"):
            in_calendar = (line.strip() == "%T\tCALENDAR")
            clndr_id_idx = clndr_name_idx = clndr_data_idx = -1
            continue
        if not in_calendar:
            continue
        if line.startswith("%F\t"):
            fields = line[3:].split("\t")
            try:
                clndr_id_idx   = fields.index("clndr_id")
                clndr_name_idx = fields.index("clndr_name")
                clndr_data_idx = fields.index("clndr_data")
            except ValueError:
                pass
            continue
        if line.startswith("%R\t") and clndr_id_idx != -1:
            fields = line[3:].split("\t")
            try:
                cal_id   = fields[clndr_id_idx].strip()
                cal_name = fields[clndr_name_idx].strip() if clndr_name_idx < len(fields) else ""
                cal_data = fields[clndr_data_idx].strip() if clndr_data_idx < len(fields) else ""
            except IndexError:
                continue
            raw_calendars[cal_id] = {"name": cal_name, "clndr_data": cal_data}

    # ── Helpers to parse a clndr_data string ─────────────────────────────────

    def _working_days(clndr_data: str) -> list:
        """Return abbreviated day names for days that have work periods."""
        if not clndr_data:
            return []
        dow_start = clndr_data.find("DaysOfWeek()(")
        if dow_start == -1:
            return []
        exc_pos  = clndr_data.find("(0||Exceptions", dow_start)
        view_pos = clndr_data.find("(0||VIEW",       dow_start)
        end = min(p for p in [exc_pos, view_pos, len(clndr_data)] if p != -1)
        dow_text = clndr_data[dow_start:end]

        working = []
        for day_num in range(1, 8):
            # Non-working days have exactly (0||N()()) — empty children list
            non_working = f"(0||{day_num}()())" in dow_text
            has_entry   = f"(0||{day_num}()("  in dow_text
            if has_entry and not non_working:
                working.append(_ABBREV[day_num])
        return working

    def _exception_dates(clndr_data: str) -> list:
        """Return sorted list of {date, name} dicts from the Exceptions section."""
        if not clndr_data:
            return []
        exc_start = clndr_data.find("(0||Exceptions()(")
        if exc_start == -1:
            return []
        exc_text = clndr_data[exc_start:]
        dates = []
        for m in _re.finditer(r'd\|(\d+)', exc_text):
            try:
                d = _P6_EPOCH + _td(days=int(m.group(1)))
                dates.append({"date": d.strftime("%Y-%m-%d"), "name": ""})
            except Exception:
                pass
        return dates

    # ── Build output ──────────────────────────────────────────────────────────
    calendars = []
    for cal_id, cal in raw_calendars.items():
        work_days  = _working_days(cal["clndr_data"])
        exceptions = _exception_dates(cal["clndr_data"])
        calendars.append({
            "id":         cal_id,
            "name":       cal["name"] or "Project Calendar",
            "work_days":  work_days,
            "exceptions": sorted(exceptions, key=lambda e: e["date"]),
        })

    return calendars


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


def _call_openai(schedule_md: str, narrative_b64: str) -> dict:
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
                        "text": f"Here is the schedule data in Markdown format:\n{schedule_md}",
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


def _call_anthropic(schedule_md: str, narrative_b64: str) -> dict:
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
                        "text": f"Here is the schedule data in Markdown format:\n{schedule_md}",
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

    # Parse XER → Markdown (used by both LLM calls)
    xer_text      = schedule_bytes.decode("utf-8", errors="ignore")
    schedule_json = parse_xer_to_json(xer_text)
    calendars     = parse_xer_calendars(xer_text)
    schedule_md   = xer_to_markdown(schedule_json, calendars)

    # Encode PDF
    narrative_b64 = base64.standard_b64encode(narrative_bytes).decode("utf-8")

    # ── Primary: GPT-4o ───────────────────────────────────────────────────────
    model_used = _OPENAI_MODEL
    try:
        result = _call_openai(schedule_md, narrative_b64)
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
            result = _call_anthropic(schedule_md, narrative_b64)
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

    return result
