"""backend/tests/test_review_endpoint.py

Tests for POST /api/review (review_endpoint) — specifically the dual upload
path added to fix Vercel's 4.5MB serverless request-body limit: a signed-in
caller can now send Storage paths (schedule_file_path, narrative_pdf_path,
...) instead of raw files, having already uploaded them directly to Storage
from the browser. The original raw-upload path (schedule_file, narrative_pdf,
...) still works unchanged for signed-out callers.

Mocks Supabase DB/storage, auth, and the review pipeline — no real network,
Neo4j, or LLM calls. Calls review_endpoint directly as a coroutine (same
pattern as test_review_pdf_endpoint.py), bypassing FastAPI's own request
parsing so each test can pass exactly the parameter combination it wants.

Runnable two ways:
    python tests/test_review_endpoint.py
    python -m pytest tests/test_review_endpoint.py
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
from app.api.review import review_endpoint, _review_progress  # noqa: E402


class _FakeUploadFile:
    """Stub for FastAPI's UploadFile — just needs an async .read()."""

    def __init__(self, content: bytes):
        self._content = content

    async def read(self) -> bytes:
        return self._content


class _FakeBucket:
    """Mimics Supabase storage bucket's download()/upload()."""

    def __init__(self, downloads: dict | None = None, raise_on_download: Exception | None = None):
        self.downloads = downloads or {}
        self.raise_on_download = raise_on_download
        self.uploaded: dict[str, bytes] = {}

    def download(self, path: str) -> bytes:
        if self.raise_on_download:
            raise self.raise_on_download
        return self.downloads[path]

    def upload(self, path: str, content: bytes, options: dict) -> None:
        self.uploaded[path] = content


class _FakeStorage:
    def __init__(self, bucket: _FakeBucket):
        self.bucket = bucket

    def from_(self, bucket_name: str) -> _FakeBucket:
        return self.bucket


class _FakeQuery:
    """Mimics Supabase's chainable table-query builder — see
    test_review_status.py's identically-shaped _FakeQuery."""

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
    def __init__(self, bucket: _FakeBucket | None = None, table_rows: list | None = None):
        self.storage = _FakeStorage(bucket or _FakeBucket())
        self._table_rows = table_rows or []

    def table(self, name):
        return _FakeQuery(self._table_rows)


def _run(coro):
    return asyncio.run(coro)


class _FakeBackgroundTasks:
    """Stub for FastAPI's BackgroundTasks — records add_task calls instead
    of running them, so a test can assert what would have been scheduled
    without the (mocked-out) pipeline actually running."""

    def __init__(self):
        self.tasks: list = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))


def _call_review_endpoint(**overrides):
    """Call review_endpoint directly as a coroutine, bypassing FastAPI's own
    request parsing (same pattern as test_review_pdf_endpoint.py). Every
    parameter with a File(...)/Form(...) default MUST be passed explicitly
    here -- calling the route function directly does not resolve those
    markers to None the way a real request through FastAPI would, so an
    omitted argument would otherwise pass the raw FieldInfo object through
    instead of the None a real request gives it."""
    kwargs = dict(
        background_tasks=_FakeBackgroundTasks(),
        schedule_file=None,
        narrative_pdf=None,
        special_provision_pdf=None,
        key_map_pdf=None,
        estimate_pdf=None,
        utility_plan_pdfs=None,
        schedule_file_path=None,
        narrative_pdf_path=None,
        special_provision_pdf_path=None,
        key_map_pdf_path=None,
        estimate_pdf_path=None,
        utility_plan_pdf_paths=None,
        project_id=None,
        checks=None,
        authorization=None,
    )
    kwargs.update(overrides)
    return review_endpoint(**kwargs)


# ── Stored-path branch ───────────────────────────────────────────────────────

def test_stored_paths_requires_signed_in_session():
    with patch("app.api.review.user_id_from_token_optional", return_value=None):
        try:
            _run(_call_review_endpoint(
                schedule_file_path="u1/p1/schedule.xer",
                narrative_pdf_path="u1/p1/narrative.pdf",
                project_id="p1",
            ))
            assert False, "Should have raised HTTPException"
        except HTTPException as e:
            assert e.status_code == 400
            assert "signed-in" in e.detail


def test_stored_paths_requires_project_id():
    with patch("app.api.review.user_id_from_token_optional", return_value="user-1"):
        try:
            _run(_call_review_endpoint(
                schedule_file_path="u1/p1/schedule.xer",
                narrative_pdf_path="u1/p1/narrative.pdf",
            ))
            assert False, "Should have raised HTTPException"
        except HTTPException as e:
            assert e.status_code == 400
            assert "project_id" in e.detail


def test_stored_paths_requires_both_schedule_and_narrative():
    with patch("app.api.review.user_id_from_token_optional", return_value="user-1"):
        try:
            _run(_call_review_endpoint(
                schedule_file_path="u1/p1/schedule.xer",
                # narrative_pdf_path omitted
                project_id="p1",
            ))
            assert False, "Should have raised HTTPException"
        except HTTPException as e:
            assert e.status_code == 400
            assert "narrative_pdf_path" in e.detail


def test_stored_paths_download_failure_returns_502():
    bucket = _FakeBucket(raise_on_download=RuntimeError("network blip"))
    with patch("app.api.review.user_id_from_token_optional", return_value="user-1"), \
         patch("app.api.review.get_db", return_value=_FakeDB(bucket)):
        try:
            _run(_call_review_endpoint(
                schedule_file_path="user-1/p1/schedule.xer",
                narrative_pdf_path="user-1/p1/narrative.pdf",
                project_id="p1",
            ))
            assert False, "Should have raised HTTPException"
        except HTTPException as e:
            assert e.status_code == 502
            assert "Failed to fetch stored files" in e.detail


def test_stored_paths_happy_path_returns_processing_and_schedules_background():
    bucket = _FakeBucket(downloads={
        "user-1/p1/schedule.xer": b"XER-BYTES",
        "user-1/p1/narrative.pdf": b"NARRATIVE-BYTES",
        "user-1/p1/special_provision.pdf": b"SP-BYTES",
    })

    with patch("app.api.review.user_id_from_token_optional", return_value="user-1"), \
         patch("app.api.review.get_db", return_value=_FakeDB(bucket)):
        bg = _FakeBackgroundTasks()
        result = _run(_call_review_endpoint(
            background_tasks=bg,
            schedule_file_path="user-1/p1/schedule.xer",
            narrative_pdf_path="user-1/p1/narrative.pdf",
            special_provision_pdf_path="user-1/p1/special_provision.pdf",
            project_id="p1",
        ))

    assert result == {"project_id": "p1", "status": "processing"}
    assert len(bg.tasks) == 1
    func, args, kwargs = bg.tasks[0]
    assert func.__name__ == "_run_review_pipeline_background"
    # Positional args: project_id, schedule_bytes, narrative_bytes, sp_bytes,
    # keymap_bytes, estimate_bytes, selected_checks, user_id,
    # utility_plan_bytes_list, schedule_path, narrative_path, sp_path,
    # keymap_path, estimate_path
    assert args[0] == "p1"
    assert args[1] == b"XER-BYTES"
    assert args[2] == b"NARRATIVE-BYTES"
    assert args[3] == b"SP-BYTES"
    assert args[7] == "user-1"          # user_id
    assert args[9] == "user-1/p1/schedule.xer"      # schedule_path
    assert args[10] == "user-1/p1/narrative.pdf"    # narrative_path
    assert args[11] == "user-1/p1/special_provision.pdf"  # sp_path
    # Nothing should have been uploaded — these paths were already there.
    assert bucket.uploaded == {}


def test_stored_paths_decodes_utility_plan_path_list():
    bucket = _FakeBucket(downloads={
        "user-1/p1/schedule.xer": b"XER",
        "user-1/p1/narrative.pdf": b"NARR",
        "user-1/p1/utility_plan_0.pdf": b"UP0",
        "user-1/p1/utility_plan_1.pdf": b"UP1",
    })

    with patch("app.api.review.user_id_from_token_optional", return_value="user-1"), \
         patch("app.api.review.get_db", return_value=_FakeDB(bucket)):
        bg = _FakeBackgroundTasks()
        _run(_call_review_endpoint(
            background_tasks=bg,
            schedule_file_path="user-1/p1/schedule.xer",
            narrative_pdf_path="user-1/p1/narrative.pdf",
            utility_plan_pdf_paths='["user-1/p1/utility_plan_0.pdf", "user-1/p1/utility_plan_1.pdf"]',
            project_id="p1",
        ))

    _, args, _ = bg.tasks[0]
    assert args[8] == [b"UP0", b"UP1"]  # utility_plan_bytes_list


def test_stored_paths_rejects_project_id_owned_by_another_review_in_memory():
    """Fix D (final-review-fixes-3-brief.md): the ownership check must also
    cover the stored-paths branch, not just raw-upload -- this is the branch
    every signed-in client's normal flow actually uses."""
    _review_progress.clear()
    _review_progress["p1"] = {"status": "running", "user_id": "owner-123"}
    try:
        with patch("app.api.review.user_id_from_token_optional", return_value="attacker-456"):
            try:
                _run(_call_review_endpoint(
                    schedule_file_path="u1/p1/schedule.xer",
                    narrative_pdf_path="u1/p1/narrative.pdf",
                    project_id="p1",
                ))
                assert False, "Should have raised HTTPException"
            except HTTPException as e:
                assert e.status_code == 403
    finally:
        _review_progress.clear()


def test_stored_paths_rejects_project_id_owned_by_another_review_via_db():
    """Same hijack, but for a project_id that finished (and dropped out of
    the in-memory dict, e.g. after a process restart) -- the ownership check
    must also fall back to the review_projects table."""
    _review_progress.clear()
    try:
        with patch("app.api.review.user_id_from_token_optional", return_value="attacker-456"), \
             patch("app.api.review.get_db", return_value=_FakeDB(table_rows=[{"user_id": "owner-123"}])):
            try:
                _run(_call_review_endpoint(
                    schedule_file_path="u1/p1/schedule.xer",
                    narrative_pdf_path="u1/p1/narrative.pdf",
                    project_id="p1",
                ))
                assert False, "Should have raised HTTPException"
            except HTTPException as e:
                assert e.status_code == 403
    finally:
        _review_progress.clear()


def test_stored_paths_allows_same_owner_to_reuse_their_own_project_id():
    """The hoisted ownership check must not block a legitimate resubmit by
    the same owner reusing their own project_id in the stored-paths branch."""
    bucket = _FakeBucket(downloads={
        "owner-123/p1/schedule.xer": b"XER-BYTES",
        "owner-123/p1/narrative.pdf": b"NARRATIVE-BYTES",
    })
    _review_progress.clear()
    _review_progress["p1"] = {"status": "running", "user_id": "owner-123"}
    try:
        with patch("app.api.review.user_id_from_token_optional", return_value="owner-123"), \
             patch("app.api.review.get_db", return_value=_FakeDB(bucket)):
            bg = _FakeBackgroundTasks()
            result = _run(_call_review_endpoint(
                background_tasks=bg,
                schedule_file_path="owner-123/p1/schedule.xer",
                narrative_pdf_path="owner-123/p1/narrative.pdf",
                project_id="p1",
            ))
        assert result == {"project_id": "p1", "status": "processing"}
    finally:
        _review_progress.clear()


def test_stored_paths_rejects_path_not_owned_by_caller():
    """Fix F (final-review-fixes-4-brief.md): a signed-in caller can supply a
    fresh, legitimately-owned project_id (passing Fix D's check) while
    pointing schedule_file_path/narrative_pdf_path/... at someone ELSE's
    Storage path -- bucket.download() runs through the service-role client
    and bypasses RLS, so without this check that other user's document gets
    downloaded and returned to the caller."""
    _review_progress.clear()
    try:
        with patch("app.api.review.user_id_from_token_optional", return_value="user-1"), \
             patch("app.api.review.get_db", return_value=_FakeDB()):
            try:
                _run(_call_review_endpoint(
                    schedule_file_path="other-user/p9/schedule.xer",
                    narrative_pdf_path="user-1/p9/narrative.pdf",
                    project_id="p9",
                ))
                assert False, "Should have raised HTTPException"
            except HTTPException as e:
                assert e.status_code == 403
    finally:
        _review_progress.clear()


def test_stored_paths_rejects_mismatched_special_provision_path():
    """Companion to the above -- proves the ownership check isn't limited to
    just schedule_file_path/narrative_pdf_path."""
    bucket = _FakeBucket(downloads={
        "user-1/p9/schedule.xer": b"XER",
        "user-1/p9/narrative.pdf": b"NARR",
    })
    _review_progress.clear()
    try:
        with patch("app.api.review.user_id_from_token_optional", return_value="user-1"), \
             patch("app.api.review.get_db", return_value=_FakeDB(bucket)):
            try:
                _run(_call_review_endpoint(
                    schedule_file_path="user-1/p9/schedule.xer",
                    narrative_pdf_path="user-1/p9/narrative.pdf",
                    special_provision_pdf_path="other-user/p9/sp.pdf",
                    project_id="p9",
                ))
                assert False, "Should have raised HTTPException"
            except HTTPException as e:
                assert e.status_code == 403
    finally:
        _review_progress.clear()


# ── Raw-upload branch (unchanged behavior, now explicitly validated) ────────

def test_raw_upload_requires_schedule_and_narrative():
    with patch("app.api.review.user_id_from_token_optional", return_value=None):
        try:
            _run(_call_review_endpoint(schedule_file=_FakeUploadFile(b"x")))  # narrative_pdf omitted
            assert False, "Should have raised HTTPException"
        except HTTPException as e:
            assert e.status_code == 400
            assert "schedule_file and narrative_pdf are required" in e.detail


def test_raw_upload_signed_out_skips_storage_entirely():
    with patch("app.api.review.user_id_from_token_optional", return_value=None), \
         patch("app.api.review.get_db") as mock_get_db:
        bg = _FakeBackgroundTasks()
        result = _run(_call_review_endpoint(
            background_tasks=bg,
            schedule_file=_FakeUploadFile(b"XER-BYTES"),
            narrative_pdf=_FakeUploadFile(b"NARRATIVE-BYTES"),
        ))

    mock_get_db.assert_not_called()  # signed-out: no Storage interaction at all
    assert result["status"] == "processing"
    _, args, _ = bg.tasks[0]
    assert args[7] is None    # user_id
    assert args[9] is None    # schedule_path
    assert args[10] is None   # narrative_path


def test_raw_upload_signed_in_uploads_and_mints_project_id():
    bucket = _FakeBucket()

    with patch("app.api.review.user_id_from_token_optional", return_value="user-1"), \
         patch("app.api.review.get_db", return_value=_FakeDB(bucket)):
        bg = _FakeBackgroundTasks()
        result = _run(_call_review_endpoint(
            background_tasks=bg,
            schedule_file=_FakeUploadFile(b"XER-BYTES"),
            narrative_pdf=_FakeUploadFile(b"NARRATIVE-BYTES"),
        ))

    minted_id = result["project_id"]
    assert minted_id  # a uuid4 string was minted since none was supplied
    assert result["status"] == "processing"
    assert bucket.uploaded[f"user-1/{minted_id}/schedule.xer"] == b"XER-BYTES"
    assert bucket.uploaded[f"user-1/{minted_id}/narrative.pdf"] == b"NARRATIVE-BYTES"
    _, args, _ = bg.tasks[0]
    assert args[9] == f"user-1/{minted_id}/schedule.xer"   # schedule_path


def test_raw_upload_rejects_project_id_owned_by_another_review():
    """A caller-supplied project_id in the raw-upload branch must not be
    allowed to hijack an in-flight review owned by someone else -- see Fix C
    in final-review-fixes-2-brief.md. Without the check, this wipes the
    legitimate owner's in-memory progress entry (review_endpoint pops it
    before writing "queued") and the owner's already-streaming EventSource
    would receive the attacker's result instead of their own."""
    _review_progress.clear()
    _review_progress["shared-id"] = {"status": "running", "user_id": "owner-123"}
    try:
        with patch("app.api.review.user_id_from_token_optional", return_value="attacker-456"):
            try:
                _run(_call_review_endpoint(
                    schedule_file=_FakeUploadFile(b"XER-BYTES"),
                    narrative_pdf=_FakeUploadFile(b"NARRATIVE-BYTES"),
                    project_id="shared-id",
                ))
                assert False, "Should have raised HTTPException"
            except HTTPException as e:
                assert e.status_code == 403
    finally:
        _review_progress.clear()


def test_raw_upload_allows_same_owner_to_reuse_their_own_project_id():
    """The Fix C ownership check must not block a legitimate retry/resubmit
    by the SAME owner reusing their own project_id."""
    bucket = _FakeBucket()
    _review_progress.clear()
    _review_progress["shared-id"] = {"status": "running", "user_id": "owner-123"}
    try:
        with patch("app.api.review.user_id_from_token_optional", return_value="owner-123"), \
             patch("app.api.review.get_db", return_value=_FakeDB(bucket)):
            bg = _FakeBackgroundTasks()
            result = _run(_call_review_endpoint(
                background_tasks=bg,
                schedule_file=_FakeUploadFile(b"XER-BYTES"),
                narrative_pdf=_FakeUploadFile(b"NARRATIVE-BYTES"),
                project_id="shared-id",
            ))
        assert result == {"project_id": "shared-id", "status": "processing"}
    finally:
        _review_progress.clear()


def test_run_review_pipeline_background_success_sets_ready():
    from app.api.review import _review_progress, _run_review_pipeline_background

    _review_progress.clear()

    def fake_pipeline(*args, **kwargs):
        return {"project_id": "p1"}

    with patch("app.api.review._run_review_pipeline", side_effect=fake_pipeline):
        _run_review_pipeline_background(
            "p1", b"XER", b"NARR", None, None, None, None, "user-1", None,
            "sched-path", "narr-path", None, None, None,
        )

    assert _review_progress["p1"]["status"] == "ready"
    assert _review_progress["p1"]["result"]["schedule_file_path"] == "sched-path"
    assert _review_progress["p1"]["result"]["narrative_pdf_path"] == "narr-path"


def test_run_review_pipeline_background_http_exception_sets_error():
    from app.api.review import _review_progress, _run_review_pipeline_background

    _review_progress.clear()

    def fake_pipeline(*args, **kwargs):
        raise HTTPException(status_code=502, detail="Compliance evaluation failed: boom")

    with patch("app.api.review._run_review_pipeline", side_effect=fake_pipeline):
        _run_review_pipeline_background(
            "p1", b"XER", b"NARR", None, None, None, None, "user-1", None,
            None, None, None, None, None,
        )

    assert _review_progress["p1"]["status"] == "error"
    assert "boom" in _review_progress["p1"]["message"]


def test_run_review_pipeline_background_generic_exception_sets_error():
    """Fix G (final-review-fixes-4-brief.md): a generic exception's raw
    str() must NOT reach the client-facing progress message -- it's streamed
    over SSE to any caller, including anonymous ones. logger.exception (not
    asserted here) still captures the real text server-side."""
    from app.api.review import _review_progress, _run_review_pipeline_background

    _review_progress.clear()

    def fake_pipeline(*args, **kwargs):
        raise RuntimeError("neo4j is down")

    with patch("app.api.review._run_review_pipeline", side_effect=fake_pipeline):
        _run_review_pipeline_background(
            "p1", b"XER", b"NARR", None, None, None, None, "user-1", None,
            None, None, None, None, None,
        )

    assert _review_progress["p1"]["status"] == "error"
    assert _review_progress["p1"]["message"] == "An unexpected error occurred while running the review."
    assert "neo4j is down" not in _review_progress["p1"]["message"]


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
