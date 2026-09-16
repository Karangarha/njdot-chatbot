"""backend/tests/test_cpm_open_ends.py

CSM Section 3.0 open-start/open-finish detection: a predecessor/successor
edge only prevents "open" status if its relationship type actually
constrains that end (FS/SS for a start, FS/FF for a finish). An SF or
FF-only predecessor does NOT prevent an open start; an SF or SS-only
successor does NOT prevent an open finish.

Runnable two ways:
    python tests/test_cpm_open_ends.py
    python -m pytest tests/test_cpm_open_ends.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.scheduling.calendar import WorkCalendar  # noqa: E402
from app.scheduling.cpm import build_network, run_cpm  # noqa: E402

_CAL = {"827": WorkCalendar(work_days=("Mon", "Tue", "Wed", "Thu", "Fri"), name="Base")}


def _activity(activity_id, pred_links=None):
    return {
        "activity_id": activity_id,
        "activity_name": activity_id,
        "wbs_path": [],
        "duration_days": 5,
        "remaining_duration_days": 5,
        "calendar_id": "827",
        "status": "Not Started",
        "task_type": "Task Dependent",
        "constraints": {},
        "pred_links": pred_links or [],
        "stored_total_float_days": None,
    }


def test_ff_only_predecessor_is_open_start():
    # B1030-shaped case: B1030's only predecessor is a Finish-to-Finish edge
    # from B1000 -- FF doesn't fix when B1030 can start, so it's open.
    activities = [
        _activity("B1000"),
        _activity("B1030", pred_links=[{"activity_id": "B1000", "type": "FF", "lag_days": 0}]),
    ]
    cpm = run_cpm(build_network(activities), _CAL, project={})
    assert "B1030" in cpm.open_starts
    # B1000 has no predecessor at all in this toy network, so it's
    # genuinely open too -- a real schedule exempts the project's own start
    # milestone by id at the compliance-check layer, not here.
    assert "B1000" in cpm.open_starts


def test_fs_predecessor_and_successor_not_open():
    activities = [
        _activity("A1000"),
        _activity("A1010", pred_links=[{"activity_id": "A1000", "type": "FS", "lag_days": 0}]),
        _activity("A1020", pred_links=[{"activity_id": "A1010", "type": "FS", "lag_days": 0}]),
    ]
    cpm = run_cpm(build_network(activities), _CAL, project={})
    assert "A1010" not in cpm.open_starts
    assert "A1010" not in cpm.open_ends


def test_sf_only_successor_is_open_end():
    activities = [
        _activity("C1000"),
        _activity("C1010", pred_links=[{"activity_id": "C1000", "type": "SF", "lag_days": 0}]),
    ]
    cpm = run_cpm(build_network(activities), _CAL, project={})
    # C1000's only outgoing relationship is SF to C1010 -- SF doesn't fix
    # C1000's finish, so C1000 has an open finish.
    assert "C1000" in cpm.open_ends


def test_ss_predecessor_is_not_open_start():
    activities = [
        _activity("D1000"),
        _activity("D1010", pred_links=[{"activity_id": "D1000", "type": "SS", "lag_days": 0}]),
    ]
    cpm = run_cpm(build_network(activities), _CAL, project={})
    assert "D1010" not in cpm.open_starts


if __name__ == "__main__":
    test_ff_only_predecessor_is_open_start()
    test_fs_predecessor_and_successor_not_open()
    test_sf_only_successor_is_open_end()
    test_ss_predecessor_is_not_open_start()
    print("All tests passed!")
