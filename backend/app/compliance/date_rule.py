"""Deterministic date/weekday/business-day-gap checks -- pure milestone-date
arithmetic that was costing an LLM call and, per the Route 49 regression,
capable of arithmetic error even when the instruction correctly named its
unit (award_to_construction's business-day-vs-calendar-day miscount).

Reads milestone dates and each milestone's own calendar straight from
Neo4j and reuses ``app.scheduling.calendar.WorkCalendar`` for business-day
counting -- the same holiday-aware calendar math the CPM engine uses, not a
re-derived approximation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Dict, Optional

from app.scheduling.calendar import DEFAULT_CALENDAR, WorkCalendar, _to_date

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


def _get_calendar(graph: Any, project_id: str, calendar_id: Optional[str]) -> WorkCalendar:
    if not calendar_id:
        return DEFAULT_CALENDAR
    rows = graph.query(
        "MATCH (c:Calendar {projectId: $pid, calendarId: $cid}) "
        "RETURN c.workDays AS workDays, c.exceptionDates AS exceptionDates, "
        "       c.workExceptionDates AS workExceptionDates, c.hoursPerDay AS hoursPerDay LIMIT 1",
        params={"pid": project_id, "cid": calendar_id},
    ) or []
    if not rows:
        return DEFAULT_CALENDAR
    r = rows[0]
    return WorkCalendar(
        work_days=r.get("workDays") or ("Mon", "Tue", "Wed", "Thu", "Fri"),
        exceptions=r.get("exceptionDates") or (),
        work_exceptions=r.get("workExceptionDates") or (),
        hours_per_day=r.get("hoursPerDay") or 8.0,
    )


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
    cal = _get_calendar(graph, project_id, a.get("calendarId"))
    gap = cal.work_days_between(a_date, b_date)
    ok = gap >= minimum
    return DateRuleResult(
        ok,
        f"{from_label} ({from_id}) = {a_date.isoformat()}, {to_label} ({to_id}) = "
        f"{b_date.isoformat()}: {gap} business day(s) on the {from_label}-side calendar "
        f"(minimum {minimum}).",
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
    sub = _get_milestone(graph, project_id, _M_SUBSTANTIAL)
    fin = _get_milestone(graph, project_id, _M_FINAL)
    sub_date = _to_date(sub.get("date")) if sub else None
    fin_date = _to_date(fin.get("date")) if fin else None
    if sub_date is None or fin_date is None:
        missing = [lbl for lbl, d in (("Substantial Completion", sub_date), ("Final Completion", fin_date)) if d is None]
        return DateRuleResult(None, f"Missing date(s) for: {', '.join(missing)}.")
    violations = [
        (lbl, d) for lbl, d in (("Substantial Completion", sub_date), ("Final Completion", fin_date))
        if _in_winter_window(d)
    ]
    ok = not violations
    detail = (
        f"Substantial Completion (M900) = {sub_date.isoformat()}, "
        f"Final Completion (M950) = {fin_date.isoformat()}."
    )
    if violations:
        detail += " In the Dec 15 - Mar 15 window: " + ", ".join(f"{lbl} ({d.isoformat()})" for lbl, d in violations) + "."
    return DateRuleResult(ok, detail)


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


# check_key -> evaluator. substantial_regional_deadlines needs `region` from
# the keymap geo result, so it's dispatched separately in eval_engine.py
# rather than through this uniform (graph, project_id) registry.
_EVALUATORS: Dict[str, Callable[[Any, str], DateRuleResult]] = {
    "ad_date_day": evaluate_ad_date_day,
    "bid_date_day": evaluate_bid_date_day,
    "ad_to_bid_gap": evaluate_ad_to_bid_gap,
    "bid_to_award_gap": evaluate_bid_to_award_gap,
    "no_completion_in_winter": evaluate_no_completion_in_winter,
}


def evaluate_date_rules(graph: Any, project_id: str, region: Optional[str]) -> Dict[str, DateRuleResult]:
    """Run every date_rule check once per review -- each is a cheap read of
    already-seeded milestone/calendar data."""
    results = {key: fn(graph, project_id) for key, fn in _EVALUATORS.items()}
    results["substantial_regional_deadlines"] = evaluate_substantial_regional_deadlines(graph, project_id, region)
    return results
