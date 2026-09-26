"""backend/tests/test_query_error_detail.py

/api/query and /api/debug must not echo exception text to the client
(sandbox finding 7). The full error still goes to the server log.

Runnable two ways:
    python backend/tests/test_query_error_detail.py
    python -m pytest backend/tests/test_query_error_detail.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.api import query as query_module  # noqa: E402
from app.models import QueryRequest  # noqa: E402

SECRET = "Error code: 500 - {'upstream': 'secret body'}"
GENERIC = "The assistant could not answer right now. Please try again."


def _detail_of(coro):
    try:
        asyncio.run(coro)
    except HTTPException as exc:
        return exc.status_code, exc.detail
    raise AssertionError("expected HTTPException")


def test_query_endpoint_hides_exception_text():
    with patch.object(query_module._expander, "expand_and_search", side_effect=RuntimeError(SECRET)):
        status, detail = _detail_of(query_module.query_endpoint(QueryRequest(query="What is a working day?")))
    assert status == 500
    assert detail == GENERIC
    assert "secret" not in detail and "RuntimeError" not in detail


def test_debug_endpoint_hides_exception_text():
    with patch.object(query_module._hybrid, "search", side_effect=RuntimeError(SECRET)):
        status, detail = _detail_of(query_module.debug_endpoint(QueryRequest(query="What is a working day?")))
    assert status == 500
    assert detail == GENERIC
    assert "secret" not in detail and "RuntimeError" not in detail


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
