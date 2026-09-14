"""Deterministic date/weekday/business-day-gap checks -- pure milestone-date
arithmetic that was costing an LLM call and, per the Route 49 regression,
capable of arithmetic error even when the instruction correctly named its
unit (award_to_construction's business-day-vs-calendar-day miscount).

Reads milestone dates from Neo4j and reuses
``app.scheduling.calendar.WorkCalendar`` for business-day counting -- the
same holiday-aware calendar math the CPM engine uses, not a re-derived
approximation. Business-day gaps use the project's designated business-day
calendar (matched by name), never a milestone's own assigned calendar --
P6 milestones are frequently linked to a 7-day calendar regardless of what
"business days" the rule actually means (confirmed on Route 49: every
administrative milestone is on "CNT0 - 4 - 7 Day Work Week").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Dict, Optional

from app.compliance.edq import _is_non_physical
from app.scheduling.calendar import WorkCalendar, _to_date

# NJDOT catalog convention: fixed milestone task ids used across every
# built-in schedule (see app.compliance.catalog's Administrative Dates /
# Completion Milestones checks, all of which already name these ids).
_M_AD, _M_BID, _M_AWARD, _M_CONSTRUCTION_START = "M100", "M200", "M300", "M500"
_M_SUBSTANTIAL, _M_FINAL = "M900", "M950"

_WEEKDAY_NAME = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass
class DateRuleResult:
    satisfied: Optional[bool]   # None -> Missing (inputs incomplete)
    detail: str
    metric_days: Optional[int] = None

    def as_dict(self) -> dict:
        return {"satisfied": self.satisfied, "detail": self.detail, "metric_days": self.metric_days}


def _get_milestone(graph: Any, project_id: str, task_id: str) -> Optional[Dict[str, Any]]:
    rows = graph.query(
        "MATCH (a:Activity {projectId: $pid, taskId: $tid}) "
        "RETURN a.taskId AS id, coalesce(a.computedEarlyStart, a.startDate) AS date, "
        "       a.calendarId AS calendarId LIMIT 1",
        params={"pid": project_id, "tid": task_id},
    ) or []
    return rows[0] if rows else None


def _calendar_from_row(r: Dict[str, Any]) -> WorkCalendar:
    return WorkCalendar(
        work_days=r.get("workDays") or ("Mon", "Tue", "Wed", "Thu", "Fri"),
        exceptions=r.get("exceptionDates") or (),
        work_exceptions=r.get("workExceptionDates") or (),
        hours_per_day=r.get("hoursPerDay") or 8.0,
        name=r.get("name") or "",
    )


_WEEKDAYS = {"Mon", "Tue", "Wed", "Thu", "Fri"}

_NO_BUSINESS_CALENDAR = (
    "No Mon-Fri business-day calendar (name containing 'Bus', with holiday "
    "exceptions seeded) was found in the schedule, so business days cannot "
    "be counted holiday-aware."
)


def _get_business_days_calendar(graph: Any, project_id: str) -> Optional[WorkCalendar]:
    """The project's designated business-day calendar (NJDOT convention:
    named e.g. "CNT0 - 1 - State Bus. Days"), used for the administrative
    business-day-gap rules -- deliberately NOT the milestone's own assigned
    calendar. Confirmed on Route 49: every milestone (M100 through M950) is
    assigned to calendar "CNT0 - 4 - 7 Day Work Week", so trusting the
    milestone's own calendar silently computed calendar days while labeled
    business days (21/21/77 instead of the correct 15/15/55) -- the exact
    unit-confusion bug this check type exists to eliminate.

    A candidate must actually be a Mon-Fri calendar and carry a seeded
    exceptionDates list (a missing property means an older seed). Ordered
    by name so two matches resolve the same way every run. Returns None --
    never a holiday-free default -- when nothing qualifies: counting
    holidays as work days only inflates the gap toward a false Pass, so
    the callers report Missing instead.
    """
    rows = graph.query(
        "MATCH (c:Calendar {projectId: $pid}) WHERE toLower(c.name) CONTAINS 'bus' "
        "RETURN c.name AS name, c.workDays AS workDays, c.exceptionDates AS exceptionDates, "
        "       c.workExceptionDates AS workExceptionDates, c.hoursPerDay AS hoursPerDay "
        "ORDER BY c.name",
        params={"pid": project_id},
    ) or []
    for r in rows:
        if r.get("exceptionDates") is None:
            continue
        if set(r.get("workDays") or ()) == _WEEKDAYS:
            return _calendar_from_row(r)
    return None


def _weekday_check(graph: Any, project_id: str, task_id: str, label: str) -> DateRuleResult:
    m = _get_milestone(graph, project_id, task_id)
    d = _to_date(m.get("date")) if m else None
    if d is None:
        return DateRuleResult(None, f"{label} milestone ({task_id}) has no resolvable date.")
    weekday = _WEEKDAY_NAME[d.weekday()]
    ok = d.weekday() in (1, 3)  # Tue=1, Thu=3
    return DateRuleResult(
        ok, f"{label} ({task_id}) = {d.isoformat()}, a {weekday}."
    )


def evaluate_ad_date_day(graph: Any, project_id: str) -> DateRuleResult:
    return _weekday_check(graph, project_id, _M_AD, "Advertisement")


def evaluate_bid_date_day(graph: Any, project_id: str) -> DateRuleResult:
    return _weekday_check(graph, project_id, _M_BID, "Bid")


def _business_day_gap(
    graph: Any, project_id: str, from_id: str, to_id: str, from_label: str, to_label: str, minimum: int,
) -> DateRuleResult:
    a = _get_milestone(graph, project_id, from_id)
    b = _get_milestone(graph, project_id, to_id)
    a_date = _to_date(a.get("date")) if a else None
    b_date = _to_date(b.get("date")) if b else None
    if a_date is None or b_date is None:
        missing = [lbl for lbl, d in ((from_label, a_date), (to_label, b_date)) if d is None]
        return DateRuleResult(None, f"Missing date(s) for: {', '.join(missing)}.")
    cal = _get_business_days_calendar(graph, project_id)
    if cal is None:
        return DateRuleResult(None, _NO_BUSINESS_CALENDAR)
    gap = cal.work_days_between(a_date, b_date)
    ok = gap >= minimum
    return DateRuleResult(
        ok,
        f"{from_label} ({from_id}) = {a_date.isoformat()}, {to_label} ({to_id}) = "
        f"{b_date.isoformat()}: {gap} business day(s) on the project's business-day "
        f"calendar '{cal.name}' (minimum {minimum}).",
        metric_days=gap,
    )


def evaluate_ad_to_bid_gap(graph: Any, project_id: str) -> DateRuleResult:
    return _business_day_gap(graph, project_id, _M_AD, _M_BID, "Advertisement", "Bid", minimum=15)


def evaluate_bid_to_award_gap(graph: Any, project_id: str) -> DateRuleResult:
    return _business_day_gap(graph, project_id, _M_BID, _M_AWARD, "Bid", "Award", minimum=15)


def _in_winter_window(d: date) -> bool:
    """Dec 15 - Mar 15, wrapping across the new year."""
    md = (d.month, d.day)
    return md >= (12, 15) or md <= (3, 15)


def evaluate_no_completion_in_winter(graph: Any, project_id: str) -> DateRuleResult:
    # Each milestone is an independent test against the window, so evaluate
    # whichever resolve -- an unresolvable Final Completion must not hide a
    # Substantial Completion that sits squarely inside Dec 15 - Mar 15.
    resolved, missing = [], []
    for lbl, tid in (("Substantial Completion", _M_SUBSTANTIAL), ("Final Completion", _M_FINAL)):
        m = _get_milestone(graph, project_id, tid)
        d = _to_date(m.get("date")) if m else None
        if d is None:
            missing.append(lbl)
        else:
            resolved.append((lbl, tid, d))
    if not resolved:
        return DateRuleResult(None, f"Missing date(s) for: {', '.join(missing)}.")
    violations = [(lbl, d) for lbl, _tid, d in resolved if _in_winter_window(d)]
    detail = ", ".join(f"{lbl} ({tid}) = {d.isoformat()}" for lbl, tid, d in resolved) + "."
    if violations:
        detail += " In the Dec 15 - Mar 15 window: " + ", ".join(f"{lbl} ({d.isoformat()})" for lbl, d in violations) + "."
    if missing:
        detail += f" Not evaluated (no resolvable date): {', '.join(missing)}."
    return DateRuleResult(not violations, detail)


def evaluate_substantial_regional_deadlines(
    graph: Any, project_id: str, region: Optional[str],
) -> DateRuleResult:
    if region not in ("NORTH", "SOUTH"):
        return DateRuleResult(None, "Project region (north/south of I-195) could not be determined from the key map.")
    m = _get_milestone(graph, project_id, _M_SUBSTANTIAL)
    d = _to_date(m.get("date")) if m else None
    if d is None:
        return DateRuleResult(None, "Substantial Completion milestone (M900) has no resolvable date.")
    deadline_md = (10, 1) if region == "NORTH" else (10, 15)
    ok = (d.month, d.day) < deadline_md
    deadline_str = f"Oct {deadline_md[1]}"
    return DateRuleResult(
        ok,
        f"Substantial Completion (M900) = {d.isoformat()}, region = {region} of I-195 "
        f"(deadline: before {deadline_str}).",
    )


_AWARD_MINIMUM_BY_TYPE = {"Federal": 55, "State": 40, "Pavement Preservation": 25}

# ponytail: keyword-based pavement-preservation classification -- a real
# NJDOT pay-item category taxonomy would be more precise than matching
# itemDescription substrings. Upgrade if this misclassifies a real project;
# until then it only fires when NO federal project number is present at all.
# Bare "pavement" is deliberately absent: it matched striping/marking/repair
# items and, since a hit LOWERS the award minimum (40 -> 25), a false
# positive here always errs toward a false Pass.
_MILLING_RESURFACING_TERMS = ("mill", "resurfac", "overlay")
_STRUCTURE_TERMS = ("bridge", "abutment", "pier", "structural steel", "beam", "deck", "culvert")

# FHWA-format federal project number as it appears in narrative prose, e.g.
# "NHP-0049(303)" -- the corroboration the Route 49 narrative carries even
# where the key sheet or DBE memo field is missing.
_FHWA_PROJECT_NO_RE = re.compile(r"\b[A-Z]{2,4}-\d{4}\(\d{3}\)")


def _narrative_federal_number(graph: Any, project_id: str) -> Optional[str]:
    rows = graph.query(
        "MATCH (c:NarrativeChunk {projectId: $pid}) RETURN c.text AS text",
        params={"pid": project_id},
    ) or []
    for r in rows:
        m = _FHWA_PROJECT_NO_RE.search(r.get("text") or "")
        if m:
            return m.group(0)
    return None


def _classify_project_type(
    graph: Any, project_id: str, federal_project_no: Optional[str],
) -> "tuple[Optional[str], str]":
    """Returns (project_type, detail). project_type is one of "Federal",
    "State", "Pavement Preservation", or None (couldn't classify -> Missing)."""
    if federal_project_no:
        return "Federal", f"Federal Project No. {federal_project_no} present."
    narrative_no = _narrative_federal_number(graph, project_id)
    if narrative_no:
        return "Federal", f"FHWA-format Federal Project No. {narrative_no} found in the designer's narrative."

    rows = graph.query(
        "MATCH (e:EdqItem {projectId: $pid}) RETURN e.itemDescription AS itemDescription",
        params={"pid": project_id},
    ) or []
    # Non-physical items (mobilization, bonds, allowances...) carry no
    # pavement/structure signal; excluding them keeps the ratio honest.
    descriptions = [
        (r.get("itemDescription") or "").lower() for r in rows
        if not _is_non_physical(r.get("itemDescription"))
    ]
    if not descriptions:
        return "State", "No Federal Project Number found; no EDQ items available to check for pavement-preservation indicators, defaulting to State."

    milling_count = sum(1 for d in descriptions if any(t in d for t in _MILLING_RESURFACING_TERMS))
    structure_count = sum(1 for d in descriptions if any(t in d for t in _STRUCTURE_TERMS))
    if structure_count == 0 and milling_count / len(descriptions) >= 0.5:
        return (
            "Pavement Preservation",
            f"No Federal Project Number; {milling_count}/{len(descriptions)} EDQ items are "
            f"milling/resurfacing-type with 0 structure items.",
        )
    return (
        "State",
        f"No Federal Project Number and no dominant pavement-preservation signature "
        f"({milling_count}/{len(descriptions)} milling/resurfacing items, "
        f"{structure_count} structure items), defaulting to State.",
    )


def evaluate_award_to_construction(
    graph: Any, project_id: str, federal_project_no: Optional[str], conflicting_federal_number: bool = False,
) -> DateRuleResult:
    # A Federal Project Number mismatch between the key map and DBE memo is a
    # real document-consistency finding, but it doesn't bear on THIS rule --
    # the rule needs to know whether the project is Federal-aid at all, not
    # which of the two numbers is correct. Confirmed on Route 49: key map
    # said 0049314, DBE memo said 0049303, and the narrative's FHWA-format
    # "NHP-0049(303)" corroborates the memo -- but either way, a federal
    # indicator being present from any source is enough to classify Federal.
    # Surface the mismatch as a note rather than blocking the verdict on it.
    conflict_note = (
        " (Note: the key map and DBE Goal Memo report different Federal "
        "Project Numbers -- worth a document-consistency check, separate "
        "from this rule.)"
        if conflicting_federal_number else ""
    )
    a = _get_milestone(graph, project_id, _M_AWARD)
    b = _get_milestone(graph, project_id, _M_CONSTRUCTION_START)
    a_date = _to_date(a.get("date")) if a else None
    b_date = _to_date(b.get("date")) if b else None
    if a_date is None or b_date is None:
        missing = [lbl for lbl, d in (("Award", a_date), ("Construction Start", b_date)) if d is None]
        return DateRuleResult(None, f"Missing date(s) for: {', '.join(missing)}.{conflict_note}")

    project_type, type_detail = _classify_project_type(graph, project_id, federal_project_no)
    if project_type is None:
        return DateRuleResult(None, f"{type_detail}{conflict_note}")

    cal = _get_business_days_calendar(graph, project_id)
    if cal is None:
        return DateRuleResult(None, f"{_NO_BUSINESS_CALENDAR}{conflict_note}")
    gap = cal.work_days_between(a_date, b_date)
    minimum = _AWARD_MINIMUM_BY_TYPE[project_type]
    ok = gap >= minimum
    detail = (
        f"{type_detail} Award (M300) = {a_date.isoformat()}, Construction Start (M500) = "
        f"{b_date.isoformat()}: {gap} business day(s) on the project's business-day "
        f"calendar '{cal.name}' (minimum {minimum} for {project_type}).{conflict_note}"
    )
    return DateRuleResult(ok, detail, metric_days=gap)


# check_key -> evaluator. substantial_regional_deadlines and
# award_to_construction need extra inputs (region, federal project number)
# beyond (graph, project_id), so they're dispatched separately in
# evaluate_date_rules rather than through this uniform registry.
_EVALUATORS: Dict[str, Callable[[Any, str], DateRuleResult]] = {
    "ad_date_day": evaluate_ad_date_day,
    "bid_date_day": evaluate_bid_date_day,
    "ad_to_bid_gap": evaluate_ad_to_bid_gap,
    "bid_to_award_gap": evaluate_bid_to_award_gap,
    "no_completion_in_winter": evaluate_no_completion_in_winter,
}


def evaluate_date_rules(
    graph: Any,
    project_id: str,
    region: Optional[str],
    federal_project_no: Optional[str] = None,
    conflicting_federal_number: bool = False,
) -> Dict[str, DateRuleResult]:
    """Run every date_rule check once per review -- each is a cheap read of
    already-seeded milestone/calendar data."""
    results = {key: fn(graph, project_id) for key, fn in _EVALUATORS.items()}
    results["substantial_regional_deadlines"] = evaluate_substantial_regional_deadlines(graph, project_id, region)
    results["award_to_construction"] = evaluate_award_to_construction(
        graph, project_id, federal_project_no, conflicting_federal_number,
    )
    return results
