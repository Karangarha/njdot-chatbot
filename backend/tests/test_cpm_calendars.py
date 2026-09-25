"""backend/tests/test_cpm_calendars.py

No XER file, no Neo4j -- a minimal synthetic two-activity network exercising
just the calendar_id -> WorkCalendar resolution in run_cpm.cal_of().

Runnable two ways:
    python tests/test_cpm_calendars.py
    python -m pytest tests/test_cpm_calendars.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.scheduling.calendar import WorkCalendar  # noqa: E402
from app.scheduling.cpm import build_network, run_cpm  # noqa: E402


def _activity(activity_id, calendar_id, duration_days=5, pred_links=None):
    return {
        "activity_id": activity_id,
        "activity_name": activity_id,
        "wbs_path": [],
        "duration_days": duration_days,
        "remaining_duration_days": duration_days,
        "calendar_id": calendar_id,
        "status": "Not Started",
        "task_type": "Task Dependent",
        "constraints": {},
        "pred_links": pred_links or [],
        "stored_total_float_days": None,
    }


def test_unresolved_calendar_id_falls_back_and_warns():
    activities = [
        _activity("A1000", calendar_id="827"),   # known calendar
        _activity("A1010", calendar_id="9999",   # unresolved -- not in `cals`
                  pred_links=[{"activity_id": "A1000", "type": "PR_FS", "lag_days": 0}]),
    ]
    net = build_network(activities)
    cals = {"827": WorkCalendar(work_days=("Mon", "Tue", "Wed", "Thu", "Fri"), name="Base")}

    cpm = run_cpm(net, cals, project={})

    # Both activities still get computed (fallback to DEFAULT_CALENDAR, not a crash).
    assert cpm.activities["A1000"].es is not None
    assert cpm.activities["A1010"].es is not None

    # The unresolved calendar_id is surfaced, not swallowed.
    assert any("9999" in w and "A1010" in w for w in cpm.warnings), cpm.warnings


def test_resolved_calendars_produce_no_unresolved_warning():
    activities = [_activity("A1000", calendar_id="827")]
    net = build_network(activities)
    cals = {"827": WorkCalendar(work_days=("Mon", "Tue", "Wed", "Thu", "Fri"), name="Base")}

    cpm = run_cpm(net, cals, project={})

    assert not any("was not found among the parsed calendars" in w for w in cpm.warnings)


if __name__ == "__main__":
    test_unresolved_calendar_id_falls_back_and_warns()
    test_resolved_calendars_produce_no_unresolved_warning()
    print("All tests passed!")
