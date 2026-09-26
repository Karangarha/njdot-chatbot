"""backend/tests/test_bdc_dates.py

Missing BDC dates must be stored as NULL, not the string "None".

Runnable two ways:
    python backend/tests/test_bdc_dates.py
    python -m pytest backend/tests/test_bdc_dates.py
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")  # module builds an OpenAI client at import

from scripts.ingest_bdc import _iso  # noqa: E402


def test_missing_date_is_none_not_string():
    assert _iso(None) is None


def test_date_is_iso_string():
    assert _iso(date(2025, 3, 1)) == "2025-03-01"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    total = sum(1 for n in globals() if n.startswith("test_"))
    print(f"\n{total - failures}/{total} passed")
