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
from app.api.review import review_endpoint  # noqa: E402


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


class _FakeDB:
    def __init__(self, bucket: _FakeBucket | None = None):
        self.storage = _FakeStorage(bucket or _FakeBucket())


def _run(coro):
    return asyncio.run(coro)


def _call_review_endpoint(**overrides):
    """Call review_endpoint directly as a coroutine, bypassing FastAPI's own
    request parsing (same pattern as test_review_pdf_endpoint.py). Every
    parameter with a File(...)/Form(...) default MUST be passed explicitly
    here -- calling the route function directly does not resolve those
    markers to None the way a real request through FastAPI would, so an
    omitted argument would otherwise pass the raw FieldInfo object through
    instead of the None a real request gives it."""
    kwargs = dict(
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
                schedule_file_path="u1/p1/schedule.xer",
                narrative_pdf_path="u1/p1/narrative.pdf",
                project_id="p1",
            ))
            assert False, "Should have raised HTTPException"
        except HTTPException as e:
            assert e.status_code == 502
            assert "Failed to fetch stored files" in e.detail


def test_stored_paths_happy_path_downloads_and_reuses_project_id():
    bucket = _FakeBucket(downloads={
        "u1/p1/schedule.xer": b"XER-BYTES",
        "u1/p1/narrative.pdf": b"NARRATIVE-BYTES",
        "u1/p1/special_provision.pdf": b"SP-BYTES",
    })
    captured = {}

    def fake_pipeline(schedule_bytes, narrative_bytes, sp_bytes, keymap_bytes, estimate_bytes,
                       selected_checks, project_id, user_id=None, utility_plan_bytes_list=None):
        captured.update(
            schedule_bytes=schedule_bytes, narrative_bytes=narrative_bytes, sp_bytes=sp_bytes,
            project_id=project_id, user_id=user_id,
            utility_plan_bytes_list=utility_plan_bytes_list,
        )
        return {"project_id": project_id}

    with patch("app.api.review.user_id_from_token_optional", return_value="user-1"), \
         patch("app.api.review.get_db", return_value=_FakeDB(bucket)), \
         patch("app.api.review._run_review_pipeline", side_effect=fake_pipeline):
        result = _run(_call_review_endpoint(
            schedule_file_path="u1/p1/schedule.xer",
            narrative_pdf_path="u1/p1/narrative.pdf",
            special_provision_pdf_path="u1/p1/special_provision.pdf",
            project_id="p1",
        ))

    assert captured["schedule_bytes"] == b"XER-BYTES"
    assert captured["narrative_bytes"] == b"NARRATIVE-BYTES"
    assert captured["sp_bytes"] == b"SP-BYTES"
    assert captured["project_id"] == "p1"  # reused, not re-minted
    assert captured["user_id"] == "user-1"
    assert captured["utility_plan_bytes_list"] == []
    assert result["schedule_file_path"] == "u1/p1/schedule.xer"
    assert result["narrative_pdf_path"] == "u1/p1/narrative.pdf"
    assert result["special_provision_pdf_path"] == "u1/p1/special_provision.pdf"
    assert result["key_map_pdf_path"] is None
    # Nothing should have been uploaded — these paths were already there.
    assert bucket.uploaded == {}


def test_stored_paths_decodes_utility_plan_path_list():
    bucket = _FakeBucket(downloads={
        "u1/p1/schedule.xer": b"XER",
        "u1/p1/narrative.pdf": b"NARR",
        "u1/p1/utility_plan_0.pdf": b"UP0",
        "u1/p1/utility_plan_1.pdf": b"UP1",
    })
    captured = {}

    def fake_pipeline(*args, utility_plan_bytes_list=None, **kwargs):
        captured["utility_plan_bytes_list"] = utility_plan_bytes_list
        return {"project_id": "p1"}

    with patch("app.api.review.user_id_from_token_optional", return_value="user-1"), \
         patch("app.api.review.get_db", return_value=_FakeDB(bucket)), \
         patch("app.api.review._run_review_pipeline", side_effect=fake_pipeline):
        _run(_call_review_endpoint(
            schedule_file_path="u1/p1/schedule.xer",
            narrative_pdf_path="u1/p1/narrative.pdf",
            utility_plan_pdf_paths='["u1/p1/utility_plan_0.pdf", "u1/p1/utility_plan_1.pdf"]',
            project_id="p1",
        ))

    assert captured["utility_plan_bytes_list"] == [b"UP0", b"UP1"]


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
    def fake_pipeline(schedule_bytes, narrative_bytes, sp_bytes, keymap_bytes, estimate_bytes,
                       selected_checks, project_id, user_id=None, utility_plan_bytes_list=None):
        return {"project_id": project_id}

    with patch("app.api.review.user_id_from_token_optional", return_value=None), \
         patch("app.api.review._run_review_pipeline", side_effect=fake_pipeline) as mock_pipeline, \
         patch("app.api.review.get_db") as mock_get_db:
        result = _run(_call_review_endpoint(
            schedule_file=_FakeUploadFile(b"XER-BYTES"),
            narrative_pdf=_FakeUploadFile(b"NARRATIVE-BYTES"),
        ))

    mock_get_db.assert_not_called()  # signed-out: no Storage interaction at all
    assert mock_pipeline.call_args.kwargs["user_id"] is None
    assert result["schedule_file_path"] is None
    assert result["narrative_pdf_path"] is None


def test_raw_upload_signed_in_uploads_and_mints_project_id():
    bucket = _FakeBucket()

    def fake_pipeline(schedule_bytes, narrative_bytes, sp_bytes, keymap_bytes, estimate_bytes,
                       selected_checks, project_id, user_id=None, utility_plan_bytes_list=None):
        return {"project_id": project_id}

    with patch("app.api.review.user_id_from_token_optional", return_value="user-1"), \
         patch("app.api.review.get_db", return_value=_FakeDB(bucket)), \
         patch("app.api.review._run_review_pipeline", side_effect=fake_pipeline):
        result = _run(_call_review_endpoint(
            schedule_file=_FakeUploadFile(b"XER-BYTES"),
            narrative_pdf=_FakeUploadFile(b"NARRATIVE-BYTES"),
        ))

    minted_id = result["project_id"]
    assert minted_id  # a uuid4 string was minted since none was supplied
    assert bucket.uploaded[f"user-1/{minted_id}/schedule.xer"] == b"XER-BYTES"
    assert bucket.uploaded[f"user-1/{minted_id}/narrative.pdf"] == b"NARRATIVE-BYTES"
    assert result["schedule_file_path"] == f"user-1/{minted_id}/schedule.xer"


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
