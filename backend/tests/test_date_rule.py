"""backend/tests/test_date_rule.py

No LLM, no real Neo4j (graph is a fake dispatching by whether the query
targets Activity or Calendar rows).

Runnable two ways:
    python tests/test_date_rule.py
    python -m pytest tests/test_date_rule.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.compliance.date_rule import (  # noqa: E402
    evaluate_ad_date_day,
    evaluate_ad_to_bid_gap,
    evaluate_award_to_construction,
    evaluate_no_completion_in_winter,
    evaluate_substantial_regional_deadlines,
)


class _FakeGraph:
    """Milestones keyed by taskId; calendars keyed by calendarId; EDQ items
    as a flat list (only used by the award_to_construction classifier)."""

    def __init__(self, milestones=None, calendars=None, edq_items=None):
        self.milestones = milestones or {}
        self.calendars = calendars or {}
        self.edq_items = edq_items or []

    def query(self, cypher, params=None):
        if "MATCH (a:Activity" in cypher:
            m = self.milestones.get(params["tid"])
            return [m] if m else []
        if "MATCH (c:Calendar" in cypher:
            c = self.calendars.get(params["cid"])
            return [c] if c else []
        if "MATCH (e:EdqItem" in cypher:
            return [{"itemDescription": d} for d in self.edq_items]
        return []


def _milestone(task_id, iso_date, calendar_id="827"):
    return {"id": task_id, "date": iso_date, "calendarId": calendar_id}


_MON_FRI_CAL = {
    "workDays": ["Mon", "Tue", "Wed", "Thu", "Fri"],
    "exceptionDates": [],
    "workExceptionDates": [],
    "hoursPerDay": 8.0,
}


def test_ad_date_day_pass_on_thursday():
    # 2024-09-05 is a Thursday.
    graph = _FakeGraph(milestones={"M100": _milestone("M100", "2024-09-05")})
    result = evaluate_ad_date_day(graph, "proj-1")
    assert result.satisfied is True
    assert "Thursday" in result.detail


def test_ad_date_day_fail_on_monday():
    # 2024-09-09 is a Monday.
    graph = _FakeGraph(milestones={"M100": _milestone("M100", "2024-09-09")})
    result = evaluate_ad_date_day(graph, "proj-1")
    assert result.satisfied is False
    assert "Monday" in result.detail


def test_ad_date_day_missing_when_no_milestone():
    result = evaluate_ad_date_day(_FakeGraph(), "proj-1")
    assert result.satisfied is None


def test_ad_to_bid_gap_uses_calendar_exceptions():
    # 2024-09-05 (Thu) to 2024-09-26 (Thu) is 15 weekdays with no holidays.
    graph = _FakeGraph(
        milestones={
            "M100": _milestone("M100", "2024-09-05"),
            "M200": _milestone("M200", "2024-09-26"),
        },
        calendars={"827": _MON_FRI_CAL},
    )
    result = evaluate_ad_to_bid_gap(graph, "proj-1")
    assert result.metric_days == 15
    assert result.satisfied is True


def test_ad_to_bid_gap_fails_below_minimum():
    graph = _FakeGraph(
        milestones={
            "M100": _milestone("M100", "2024-09-05"),
            "M200": _milestone("M200", "2024-09-12"),  # only 5 weekdays later
        },
        calendars={"827": _MON_FRI_CAL},
    )
    result = evaluate_ad_to_bid_gap(graph, "proj-1")
    assert result.satisfied is False
    assert result.metric_days < 15


def test_ad_to_bid_gap_excludes_holiday_exceptions():
    # Same 15-weekday window as the passing test above, but with one holiday
    # exception inside it -- the holiday-aware count must drop to 14 and fail.
    cal_with_holiday = {
        **_MON_FRI_CAL,
        "exceptionDates": ["2024-09-16"],  # a Monday inside the window
    }
    graph = _FakeGraph(
        milestones={
            "M100": _milestone("M100", "2024-09-05"),
            "M200": _milestone("M200", "2024-09-26"),
        },
        calendars={"827": cal_with_holiday},
    )
    result = evaluate_ad_to_bid_gap(graph, "proj-1")
    assert result.metric_days == 14
    assert result.satisfied is False


def test_no_completion_in_winter_fails_when_either_milestone_in_window():
    graph = _FakeGraph(milestones={
        "M900": _milestone("M900", "2026-01-15"),   # inside Dec15-Mar15
        "M950": _milestone("M950", "2026-06-01"),
    })
    result = evaluate_no_completion_in_winter(graph, "proj-1")
    assert result.satisfied is False
    assert "Substantial Completion" in result.detail


def test_no_completion_in_winter_passes_outside_window():
    graph = _FakeGraph(milestones={
        "M900": _milestone("M900", "2026-08-05"),
        "M950": _milestone("M950", "2026-10-28"),
    })
    result = evaluate_no_completion_in_winter(graph, "proj-1")
    assert result.satisfied is True


def test_substantial_regional_deadlines_south_before_oct15():
    graph = _FakeGraph(milestones={"M900": _milestone("M900", "2026-08-05")})
    result = evaluate_substantial_regional_deadlines(graph, "proj-1", "SOUTH")
    assert result.satisfied is True


def test_substantial_regional_deadlines_missing_when_region_unresolved():
    graph = _FakeGraph(milestones={"M900": _milestone("M900", "2026-08-05")})
    result = evaluate_substantial_regional_deadlines(graph, "proj-1", None)
    assert result.satisfied is None


def _award_graph(award_date, construction_start_date, edq_items=None):
    return _FakeGraph(
        milestones={
            "M300": _milestone("M300", award_date),
            "M500": _milestone("M500", construction_start_date),
        },
        calendars={"827": _MON_FRI_CAL},
        edq_items=edq_items or [],
    )


def test_award_to_construction_federal_passes_at_55_business_days():
    # Route 49 fixture shape: Award 2024-09-05 -> Construction Start
    # 2024-11-21 is >= 55 weekdays even on a plain Mon-Fri calendar with no
    # holiday exceptions (the real holiday-aware count is exactly 55; the
    # plain count is a few days higher, which still clears the minimum).
    graph = _award_graph("2024-09-05", "2024-11-21")
    result = evaluate_award_to_construction(graph, "proj-1", federal_project_no="0123456")
    assert result.satisfied is True
    assert "Federal" in result.detail
    assert result.metric_days >= 55


def test_award_to_construction_fails_below_federal_minimum():
    graph = _award_graph("2024-09-05", "2024-09-19")  # ~10 weekdays
    result = evaluate_award_to_construction(graph, "proj-1", federal_project_no="0123456")
    assert result.satisfied is False


def test_award_to_construction_defaults_to_state_without_federal_number_or_edq_items():
    graph = _award_graph("2024-09-05", "2024-11-21", edq_items=[])
    result = evaluate_award_to_construction(graph, "proj-1", federal_project_no=None)
    assert "State" in result.detail
    assert result.satisfied is True  # 40-day minimum, well cleared


def test_award_to_construction_classifies_pavement_preservation_from_edq_items():
    items = ["Mill Existing Pavement", "HMA Resurfacing Course", "Pavement Striping"] * 3
    graph = _award_graph("2024-09-05", "2024-10-10", edq_items=items)  # short gap, ok for 25-day min
    result = evaluate_award_to_construction(graph, "proj-1", federal_project_no=None)
    assert "Pavement Preservation" in result.detail
    assert result.satisfied is True


def test_award_to_construction_missing_on_conflicting_federal_numbers():
    graph = _award_graph("2024-09-05", "2024-11-21")
    result = evaluate_award_to_construction(
        graph, "proj-1", federal_project_no="0123456", conflicting_federal_number=True,
    )
    assert result.satisfied is None


if __name__ == "__main__":
    test_ad_date_day_pass_on_thursday()
    test_ad_date_day_fail_on_monday()
    test_ad_date_day_missing_when_no_milestone()
    test_ad_to_bid_gap_uses_calendar_exceptions()
    test_ad_to_bid_gap_fails_below_minimum()
    test_ad_to_bid_gap_excludes_holiday_exceptions()
    test_no_completion_in_winter_fails_when_either_milestone_in_window()
    test_no_completion_in_winter_passes_outside_window()
    test_substantial_regional_deadlines_south_before_oct15()
    test_substantial_regional_deadlines_missing_when_region_unresolved()
    test_award_to_construction_federal_passes_at_55_business_days()
    test_award_to_construction_fails_below_federal_minimum()
    test_award_to_construction_defaults_to_state_without_federal_number_or_edq_items()
    test_award_to_construction_classifies_pavement_preservation_from_edq_items()
    test_award_to_construction_missing_on_conflicting_federal_numbers()
    print("All tests passed!")
