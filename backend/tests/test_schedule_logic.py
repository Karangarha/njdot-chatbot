"""backend/tests/test_schedule_logic.py

No LLM, no network, no real Neo4j (graph is a fake dispatching by a
substring unique to each real query in app.compliance.schedule_logic).

Runnable two ways:
    python tests/test_schedule_logic.py
    python -m pytest tests/test_schedule_logic.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.compliance.schedule_logic import (  # noqa: E402
    EXEMPT_MILESTONE_IDS,
    evaluate_no_lag,
    evaluate_no_mandatory_constraints,
    evaluate_no_negative_float,
    evaluate_no_open_ends,
)


class _FakeGraph:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def query(self, cypher, params=None):
        self.calls.append((cypher, params))
        return self.rows


def test_no_negative_float_pass_when_empty():
    result = evaluate_no_negative_float(_FakeGraph(rows=[]), "proj-1")
    assert result.violations == []
    assert "No activities with negative float" in result.detail


def test_no_negative_float_fail_lists_activities():
    result = evaluate_no_negative_float(
        _FakeGraph(rows=[{"id": "D1080", "name": "Demo", "totalFloat": -5}]), "proj-1",
    )
    assert len(result.violations) == 1
    assert "D1080" in result.detail and "-5" in result.detail


def test_mandatory_constraints_filters_to_mandatory_types_only():
    # CS_ALAP/CS_FNLT are real P6 constraint codes but NOT mandatory ones --
    # only CS_MANDSTART/CS_MSO/CS_MANDFIN/CS_MEO should count as violations.
    rows = [
        {"id": "M950", "type": "CS_FNLT", "date": "2026-10-28"},
        {"id": "A1000", "type": "CS_MANDSTART", "date": "2024-11-21"},
    ]
    result = evaluate_no_mandatory_constraints(_FakeGraph(rows=rows), "proj-1")
    assert len(result.violations) == 1
    assert result.violations[0]["id"] == "A1000"


def test_no_lag_flags_fs_lag_and_negative_lag_only():
    rows = [
        {"pred": "A1", "succ": "A2", "relType": "FS", "lagDays": 3},    # FS lag -- prohibited
        {"pred": "B1", "succ": "B2", "relType": "SS", "lagDays": 2},    # SS positive lag -- fine
        {"pred": "C1", "succ": "C2", "relType": "FF", "lagDays": -1},   # negative lag any type -- prohibited
        {"pred": "D1", "succ": "D2", "relType": "FS", "lagDays": 0},    # no lag -- fine
    ]
    result = evaluate_no_lag(_FakeGraph(rows=rows), "proj-1")
    violators = {(v["pred"], v["succ"]) for v in result.violations}
    assert violators == {("A1", "A2"), ("C1", "C2")}


def test_no_lag_does_not_truncate_large_relationship_sets():
    # Regression: the raw classification fetch used to LIMIT $limit at
    # _MAX_ROWS*4=160. A real 136-activity schedule can carry 240+
    # PRECEDES edges (P6 allows several relationship types between one
    # pair), so that limit silently dropped violations past row 160 on
    # the Route 49 fixture (12 real FS-lag violations, only 11 returned).
    # No LIMIT should appear in the query at all now.
    rows = [{"pred": f"P{i}", "succ": f"S{i}", "relType": "FS", "lagDays": 1} for i in range(300)]
    graph = _FakeGraph(rows=rows)
    result = evaluate_no_lag(graph, "proj-1")
    assert len(result.violations) == 300
    cypher, _params = graph.calls[0]
    assert "LIMIT" not in cypher.upper()


def test_no_open_ends_exempts_project_milestones():
    rows = [
        {"id": "M100", "name": "Advertise", "isOpenStart": True, "isOpenEnd": False},
        {"id": "M950", "name": "Completion", "isOpenStart": False, "isOpenEnd": True},
        {"id": "B1030", "name": "Place MPT", "isOpenStart": True, "isOpenEnd": False},
    ]
    result = evaluate_no_open_ends(_FakeGraph(rows=rows), "proj-1")
    ids = {v["id"] for v in result.violations}
    assert ids == {"B1030"}
    assert EXEMPT_MILESTONE_IDS == {"M100", "M950"}


if __name__ == "__main__":
    test_no_negative_float_pass_when_empty()
    test_no_negative_float_fail_lists_activities()
    test_mandatory_constraints_filters_to_mandatory_types_only()
    test_no_lag_flags_fs_lag_and_negative_lag_only()
    test_no_lag_does_not_truncate_large_relationship_sets()
    test_no_open_ends_exempts_project_milestones()
    print("All tests passed!")
