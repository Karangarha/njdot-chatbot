"""backend/tests/test_review_rerun_endpoint.py

Tests for POST /api/review/{project_id}/rerun (review_rerun_endpoint) after
moving its pipeline execution to a background task. Mocks Supabase and the
review pipeline — no real network, Neo4j, or LLM calls.

Runnable two ways:
    python tests/test_review_rerun_endpoint.py
    python -m pytest tests/test_review_rerun_endpoint.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import HTTPException  # noqa: E402
from app.api.review import (  # noqa: E402
    _review_progress,
    _run_review_rerun_background,
    review_rerun_endpoint,
)


class _FakeBackgroundTasks:
    def __init__(self):
        self.tasks: list = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))


class _FakeExecuted:
    def __init__(self, data):
        self.data = data


class _FakeTable:
    """Mimics Supabase's table() chain for both select and update calls."""

    def __init__(self, row):
        self._row = row
        self.updates: list = []

    def select(self, *args, **kwargs):
        return self

    def update(self, fields):
        self.updates.append(fields)
        return self

    def eq(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def execute(self):
        return _FakeExecuted([self._row] if self._row else [])


class _FakeBucket:
    def __init__(self, downloads):
        self._downloads = downloads

    def download(self, path):
        return self._downloads[path]


class _FakeStorage:
    def __init__(self, bucket):
        self._bucket = bucket

    def from_(self, name):
        return self._bucket


class _FakeDB:
    def __init__(self, row, downloads=None):
        self._table = _FakeTable(row)
        self.storage = _FakeStorage(_FakeBucket(downloads or {}))

    def table(self, name):
        return self._table


def _run(coro):
    return asyncio.run(coro)


def setup_function(_fn):
    _review_progress.clear()


ROW = {
    "id": "p1",
    "user_id": "user-1",
    "project_name": "Old Name",
    "schedule_file_path": "user-1/p1/schedule.xer",
    "narrative_pdf_path": "user-1/p1/narrative.pdf",
    "special_provision_pdf_path": None,
    "key_map_pdf_path": None,
    "estimate_pdf_path": None,
    "key_map_extraction": None,
    "estimate_extraction": None,
}


def test_rerun_returns_processing_and_schedules_background():
    db = _FakeDB(ROW, downloads={"user-1/p1/schedule.xer": b"XER", "user-1/p1/narrative.pdf": b"NARR"})
    with patch("app.api.review.user_id_from_token", return_value="user-1"), \
         patch("app.api.review.get_db", return_value=db):
        bg = _FakeBackgroundTasks()
        result = _run(review_rerun_endpoint(
            project_id="p1", background_tasks=bg, authorization="Bearer token", checks=None,
        ))

    assert result == {"project_id": "p1", "status": "processing"}
    assert len(bg.tasks) == 1
    func, args, _ = bg.tasks[0]
    assert func.__name__ == "_run_review_rerun_background"
    assert args[0] == "p1"
    assert args[1] == b"XER"
    assert args[2] == b"NARR"


def test_rerun_project_not_found_returns_404():
    db = _FakeDB(None)
    with patch("app.api.review.user_id_from_token", return_value="user-1"), \
         patch("app.api.review.get_db", return_value=db):
        try:
            _run(review_rerun_endpoint(
                project_id="missing", background_tasks=_FakeBackgroundTasks(),
                authorization="Bearer token", checks=None,
            ))
            assert False, "Should have raised HTTPException"
        except HTTPException as e:
            assert e.status_code == 404


def test_rerun_user_mismatch_returns_403():
    db = _FakeDB({**ROW, "user_id": "someone-else"})
    with patch("app.api.review.user_id_from_token", return_value="user-1"), \
         patch("app.api.review.get_db", return_value=db):
        try:
            _run(review_rerun_endpoint(
                project_id="p1", background_tasks=_FakeBackgroundTasks(),
                authorization="Bearer token", checks=None,
            ))
            assert False, "Should have raised HTTPException"
        except HTTPException as e:
            assert e.status_code == 403


def test_rerun_rejects_row_with_mismatched_narrative_path():
    """Fix H (final-review-fixes-5-brief.md): the existing ownership check
    only confirms the review_projects ROW belongs to the caller. The path
    COLUMNS on that row are written by a plain client-side Supabase insert
    (frontend/src/components/review/DocumentReview.tsx) using the
    anon/authenticated client, not this backend -- nothing stops a caller
    from using the Supabase JS client directly to insert a row under their
    own user_id but with a DIFFERENT user's storage path in a path column.
    Without validating the path itself, bucket.download() (service-role,
    bypasses RLS) would fetch the other user's file."""
    row = {**ROW, "narrative_pdf_path": "other-user/proj/narrative.pdf"}
    db = _FakeDB(row, downloads={
        "u1/p1/schedule.xer": b"XER",
        "other-user/proj/narrative.pdf": b"NARR",
    })
    with patch("app.api.review.user_id_from_token", return_value="user-1"), \
         patch("app.api.review.get_db", return_value=db):
        try:
            _run(review_rerun_endpoint(
                project_id="p1", background_tasks=_FakeBackgroundTasks(),
                authorization="Bearer token", checks=None,
            ))
            assert False, "Should have raised HTTPException"
        except HTTPException as e:
            assert e.status_code == 403


def test_rerun_rejects_percent_encoded_dotdot_traversal_in_row_path():
    """Fix J (final-review-fixes-6-brief.md): round 5's _validate_owned_path
    only rejected a LITERAL '..' path segment via raw-string splitting.
    "%2e%2e" is not the string '..', so it passed that check -- but
    storage3's actual yarl-based request building percent-decodes it to
    '..' and resolves it out of the owner's directory. A DB row whose path
    column was populated (via the client-side Supabase insert, not this
    backend -- see test_rerun_rejects_row_with_mismatched_narrative_path
    above) with such a payload must still be rejected with a 403."""
    row = {**ROW, "narrative_pdf_path": "user-1/%2e%2e/victim/proj/narrative.pdf"}
    db = _FakeDB(row, downloads={"user-1/p1/schedule.xer": b"XER"})
    with patch("app.api.review.user_id_from_token", return_value="user-1"), \
         patch("app.api.review.get_db", return_value=db):
        try:
            _run(review_rerun_endpoint(
                project_id="p1", background_tasks=_FakeBackgroundTasks(),
                authorization="Bearer token", checks=None,
            ))
            assert False, "Should have raised HTTPException"
        except HTTPException as e:
            assert e.status_code == 403


def test_run_review_rerun_background_success_updates_db_and_sets_ready():
    db = _FakeDB(ROW)

    def fake_pipeline(*args, **kwargs):
        return {"project_id": "p1", "project_name": "New Name", "key_map": {}, "estimate": {}}

    with patch("app.api.review.get_db", return_value=db), \
         patch("app.api.review._run_review_pipeline", side_effect=fake_pipeline):
        _run_review_rerun_background("p1", b"XER", b"NARR", None, None, None, None, "user-1", ROW)

    assert _review_progress["p1"]["status"] == "ready"
    assert _review_progress["p1"]["result"]["project_name"] == "New Name"
    assert len(db._table.updates) == 1
    assert db._table.updates[0]["project_name"] == "New Name"


def test_run_review_rerun_background_failure_sets_error():
    """Fix G (final-review-fixes-4-brief.md): a generic exception's raw
    str() must NOT reach the client-facing progress message -- it's streamed
    over SSE to any caller, including anonymous ones. logger.exception (not
    asserted here) still captures the real text server-side."""
    db = _FakeDB(ROW)

    def fake_pipeline(*args, **kwargs):
        raise RuntimeError("neo4j is down")

    with patch("app.api.review.get_db", return_value=db), \
         patch("app.api.review._run_review_pipeline", side_effect=fake_pipeline):
        _run_review_rerun_background("p1", b"XER", b"NARR", None, None, None, None, "user-1", ROW)

    assert _review_progress["p1"]["status"] == "error"
    assert _review_progress["p1"]["message"] == "An unexpected error occurred while running the review."
    assert "neo4j is down" not in _review_progress["p1"]["message"]
    assert len(db._table.updates) == 0  # never reached the DB update


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
