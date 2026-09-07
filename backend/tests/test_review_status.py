"""backend/tests/test_review_status.py

Tests for GET /api/review/{project_id}/status (review_status) — the SSE
progress stream a review's background task reports into. Mocks Supabase;
no real network, Neo4j, or LLM calls.

Runnable two ways:
    python tests/test_review_status.py
    python -m pytest tests/test_review_status.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.api.review import _review_progress, _set_review_progress, review_status  # noqa: E402


class _FakeQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def execute(self):
        return type("Executed", (), {"data": self._data})()


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _FakeQuery(self._rows)


async def _collect_events(response, max_events: int = 5):
    """Consume a StreamingResponse's async generator up to max_events (a
    safety cap — every test here expects the stream to close on its own
    well before that via a ready/error status)."""
    events = []
    async for chunk in response.body_iterator:
        events.append(chunk)
        if len(events) >= max_events:
            break
    return events


def setup_function(_fn):
    _review_progress.clear()


def test_set_review_progress_merges_updates():
    _set_review_progress("p1", status="queued", message="Queued…")
    _set_review_progress("p1", status="running")

    assert _review_progress["p1"] == {"status": "running", "message": "Queued…"}


def test_review_status_streams_ready_immediately():
    _set_review_progress("p1", status="ready", message="Review complete.", result={"project_id": "p1"})

    response = asyncio.run(review_status("p1"))
    events = asyncio.run(_collect_events(response))

    assert len(events) == 1
    assert '"status": "ready"' in events[0]
    assert '"project_id": "p1"' in events[0]


def test_review_status_streams_error_immediately():
    _set_review_progress("p1", status="error", message="Compliance evaluation failed: boom")

    response = asyncio.run(review_status("p1"))
    events = asyncio.run(_collect_events(response))

    assert len(events) == 1
    assert '"status": "error"' in events[0]
    assert "boom" in events[0]


def test_review_status_falls_back_to_supabase_when_not_in_memory():
    stored_result = {"project_id": "p2", "summary": {"passed": 10}}
    with patch("app.api.review.get_db", return_value=_FakeDB([{"review_result": stored_result}])):
        response = asyncio.run(review_status("p2"))
        events = asyncio.run(_collect_events(response))

    assert len(events) == 1
    assert '"status": "ready"' in events[0]
    assert '"passed": 10' in events[0]


def test_review_status_reports_error_when_truly_not_found():
    with patch("app.api.review.get_db", return_value=_FakeDB([])):
        response = asyncio.run(review_status("nonexistent"))
        events = asyncio.run(_collect_events(response))

    assert len(events) == 1
    assert '"status": "error"' in events[0]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                setup_function(fn)
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    total = sum(1 for n in globals() if n.startswith("test_"))
    print(f"\n{total - failures}/{total} passed")
