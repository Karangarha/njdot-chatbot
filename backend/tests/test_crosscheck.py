"""backend/tests/test_crosscheck.py

No LLM, no Neo4j, no real XER -- CpmResult/CpmActivityResult built by hand.

Runnable two ways:
    python tests/test_crosscheck.py
    python -m pytest tests/test_crosscheck.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.scheduling.crosscheck import cross_check  # noqa: E402
from app.scheduling.cpm import CpmActivityResult, CpmResult  # noqa: E402


def _act(activity_id, **overrides):
    base = {
        "activity_id": activity_id,
        "status": "Not Started",
        "stored_total_float_days": 10,
        "stored_free_float_days": 10,
        "early_start": "2025-01-01",
        "early_finish": "2025-01-10",
        "late_start": "2025-01-05",
        "late_finish": "2025-01-15",
    }
    base.update(overrides)
    return base


def _result(activity_id, **overrides):
    base = dict(
        activity_id=activity_id,
        es=date(2025, 1, 1), ef=date(2025, 1, 10),
        ls=date(2025, 1, 5), lf=date(2025, 1, 15),
        total_float=10, free_float=10, is_critical=False,
    )
    base.update(overrides)
    return CpmActivityResult(**base)


def test_free_float_only_delta_does_not_fail_the_activity():
    # D1070-shaped case: total float and every date agree with P6, only
    # free float differs (0 computed vs 43 stored) -- this must NOT appear
    # in `mismatches` (which drives hasMismatch / the cpm_consistency Fail),
    # only in the separate informational `free_float_notes` list.
    activities = [_act("D1040")]
    cpm = CpmResult(activities={"D1040": _result("D1040", free_float=0)})
    result = cross_check(activities, cpm)
    assert result["mismatches"] == []
    assert result["summary"]["clean"] == 1
    assert len(result["free_float_notes"]) == 1
    assert result["free_float_notes"][0]["activity_id"] == "D1040"
    assert result["summary"]["free_float_notes"] == 1


def test_total_float_delta_still_fails_the_activity():
    activities = [_act("A1000", stored_total_float_days=10)]
    cpm = CpmResult(activities={"A1000": _result("A1000", total_float=25)})
    result = cross_check(activities, cpm)
    assert len(result["mismatches"]) == 1
    assert result["mismatches"][0]["field"] == "total_float"
    assert result["summary"]["clean"] == 0


def test_date_delta_still_fails_the_activity():
    activities = [_act("B1000", early_start="2025-01-01")]
    cpm = CpmResult(activities={"B1000": _result("B1000", es=date(2025, 1, 20))})
    result = cross_check(activities, cpm)
    fields = {m["field"] for m in result["mismatches"]}
    assert "early_start" in fields
    assert result["summary"]["clean"] == 0


def test_clean_activity_reports_no_notes_or_mismatches():
    activities = [_act("C1000")]
    cpm = CpmResult(activities={"C1000": _result("C1000")})
    result = cross_check(activities, cpm)
    assert result["mismatches"] == []
    assert result["free_float_notes"] == []
    assert result["summary"]["clean"] == 1


if __name__ == "__main__":
    test_free_float_only_delta_does_not_fail_the_activity()
    test_total_float_delta_still_fails_the_activity()
    test_date_delta_still_fails_the_activity()
    test_clean_activity_reports_no_notes_or_mismatches()
    print("All tests passed!")
