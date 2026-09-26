"""backend/tests/test_observability_quiet.py

With Langfuse unconfigured, tracing must be skipped silently instead of
warning on every LLM call; with it configured, tracing must still turn on.

Runnable two ways:
    python backend/tests/test_observability_quiet.py
    python -m pytest backend/tests/test_observability_quiet.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import observability  # noqa: E402
from tests.logcapture import capture_logs  # noqa: E402

_UNSET = {"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""}
_SET = {"LANGFUSE_PUBLIC_KEY": "pk-test", "LANGFUSE_SECRET_KEY": "sk-test"}


def test_unconfigured_langfuse_returns_none_without_warning():
    with patch.dict(os.environ, _UNSET), \
         capture_logs("langfuse") as lf_records, \
         capture_logs("app.observability") as our_records:
        for _ in range(3):
            assert observability.get_langfuse_handler() is None
            assert observability.new_trace_id("seed") is None
            assert observability.get_langfuse_client() is None
    assert lf_records == [], [r.getMessage() for r in lf_records]
    assert our_records == [], [r.getMessage() for r in our_records]


def test_configured_langfuse_still_builds_handler():
    sentinel = object()
    with patch.dict(os.environ, _SET), \
         patch("langfuse.langchain.CallbackHandler", return_value=sentinel):
        assert observability.get_langfuse_handler() is sentinel


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
