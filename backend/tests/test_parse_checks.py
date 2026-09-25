"""backend/tests/test_parse_checks.py

FINDING 5: app.api.review._parse_checks's `item.get("sp_top_k") or 8` only
catches 0/None -- a negative value (e.g. -5) is truthy in Python and would
pass straight through as check_retrieval.retrieve_for_check's pin budget.
A custom check's sp_top_k must fall back to 8 for anything that isn't a
positive int.

    python -m pytest backend/tests/test_parse_checks.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.api.review import _parse_checks  # noqa: E402


def _custom_check(**overrides):
    item = {
        "check_key": "my_custom_check", "category": "Cat", "name": "My Check",
        "instruction": "do it",
    }
    item.update(overrides)
    return json.dumps([item])


def test_negative_sp_top_k_falls_back_to_default():
    checks = _parse_checks(_custom_check(sp_top_k=-5))
    assert checks[0].sp_top_k == 8


def test_zero_sp_top_k_falls_back_to_default():
    checks = _parse_checks(_custom_check(sp_top_k=0))
    assert checks[0].sp_top_k == 8


def test_positive_sp_top_k_is_kept():
    checks = _parse_checks(_custom_check(sp_top_k=12))
    assert checks[0].sp_top_k == 12


def test_missing_sp_top_k_falls_back_to_default():
    checks = _parse_checks(_custom_check())
    assert checks[0].sp_top_k == 8


def test_non_int_sp_top_k_falls_back_to_default():
    checks = _parse_checks(_custom_check(sp_top_k="12"))
    assert checks[0].sp_top_k == 8


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("All tests passed!")
