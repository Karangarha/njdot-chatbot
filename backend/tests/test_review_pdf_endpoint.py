"""backend/tests/test_review_pdf_endpoint.py

Tests for the review PDF serving endpoint (GET /api/review/{project_id}/pdf/{doc_type}).

Mocks Supabase DB and storage access — no real network or LLM calls.
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
from app.api.review import review_pdf_endpoint  # noqa: E402


class _FakeExecuted:
    """Mimics the result of a Supabase query execution."""
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Mimics a Supabase query builder (select/eq/limit/execute chain)."""
    def __init__(self, data):
        self._data = data

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def execute(self):
        return _FakeExecuted(self._data)


class _FakeBucket:
    """Mimics Supabase storage bucket with download() method."""
    def __init__(self, pdf_bytes=None, raise_error=None):
        self.pdf_bytes = pdf_bytes or b"fake pdf content"
        self.raise_error = raise_error

    def download(self, path):
        if self.raise_error:
            raise self.raise_error
        return self.pdf_bytes


class _FakeStorage:
    """Mimics Supabase storage.from_() interface."""
    def __init__(self, bucket):
        self.bucket = bucket

    def from_(self, bucket_name):
        return self.bucket


class _FakeDB:
    """Mimics Supabase client with table() and storage interfaces."""
    def __init__(self, table_data=None, bucket=None):
        # If table_data is None or an empty dict, treat as "no rows found"
        if table_data is None or (isinstance(table_data, dict) and not table_data):
            self.table_data = None
        else:
            self.table_data = table_data
        self.bucket = bucket or _FakeBucket()
        self.storage = _FakeStorage(self.bucket)

    def table(self, name):
        if self.table_data is None:
            return _FakeQuery([])
        return _FakeQuery([self.table_data])


def test_unknown_doc_type_returns_404():
    """Unknown doc_type should return 404."""
    async def run_test():
        with patch("app.api.review.get_db") as mock_get_db:
            mock_get_db.return_value = _FakeDB()
            try:
                await review_pdf_endpoint("proj-1", "unknown_type", "Bearer token")
                assert False, "Should have raised HTTPException"
            except HTTPException as e:
                assert e.status_code == 404
                assert "Unknown document type" in e.detail
    asyncio.run(run_test())


def test_no_authorization_header_returns_401():
    """Missing authorization header should raise 401 via user_id_from_token."""
    async def run_test():
        with patch("app.api.review.user_id_from_token") as mock_token:
            mock_token.side_effect = HTTPException(status_code=401, detail="Unauthorized")
            try:
                await review_pdf_endpoint("proj-1", "narrative", None)
                assert False, "Should have raised HTTPException"
            except HTTPException as e:
                assert e.status_code == 401
    asyncio.run(run_test())


def test_project_not_found_returns_404():
    """Project ID not in DB should return 404."""
    async def run_test():
        with patch("app.api.review.get_db") as mock_get_db, \
             patch("app.api.review.user_id_from_token") as mock_token:
            mock_token.return_value = "user-123"
            mock_get_db.return_value = _FakeDB(table_data={})  # Empty project
            try:
                await review_pdf_endpoint("nonexistent-proj", "narrative", "Bearer token")
                assert False, "Should have raised HTTPException"
            except HTTPException as e:
                assert e.status_code == 404
                assert "Review project not found" in e.detail
    asyncio.run(run_test())


def test_user_mismatch_returns_403():
    """If row.user_id != user_id, should return 403."""
    async def run_test():
        with patch("app.api.review.get_db") as mock_get_db, \
             patch("app.api.review.user_id_from_token") as mock_token:
            mock_token.return_value = "user-123"
            mock_get_db.return_value = _FakeDB(
                table_data={"id": "proj-1", "user_id": "different-user"}
            )
            try:
                await review_pdf_endpoint("proj-1", "narrative", "Bearer token")
                assert False, "Should have raised HTTPException"
            except HTTPException as e:
                assert e.status_code == 403
                assert "does not belong to you" in e.detail
    asyncio.run(run_test())


def test_no_path_stored_returns_404():
    """Valid doc_type but no path stored (column is None/empty) should return 404."""
    async def run_test():
        with patch("app.api.review.get_db") as mock_get_db, \
             patch("app.api.review.user_id_from_token") as mock_token:
            mock_token.return_value = "user-123"
            mock_get_db.return_value = _FakeDB(
                table_data={"id": "proj-1", "user_id": "user-123", "narrative_pdf_path": None}
            )
            try:
                await review_pdf_endpoint("proj-1", "narrative", "Bearer token")
                assert False, "Should have raised HTTPException"
            except HTTPException as e:
                assert e.status_code == 404
                assert "No narrative PDF stored" in e.detail
    asyncio.run(run_test())


def test_happy_path_returns_pdf_bytes():
    """Valid project with stored PDF should return 200 with correct headers and content."""
    async def run_test():
        pdf_content = b"fake pdf bytes here"
        bucket = _FakeBucket(pdf_bytes=pdf_content)

        with patch("app.api.review.get_db") as mock_get_db, \
             patch("app.api.review.user_id_from_token") as mock_token:
            mock_token.return_value = "user-123"
            db_mock = _FakeDB(
                table_data={
                    "id": "proj-1",
                    "user_id": "user-123",
                    "narrative_pdf_path": "user-123/proj-1/narrative.pdf",
                },
                bucket=bucket,
            )
            mock_get_db.return_value = db_mock

            response = await review_pdf_endpoint("proj-1", "narrative", "Bearer token")

            assert response.status_code == 200
            assert response.media_type == "application/pdf"
            assert response.body == pdf_content
            assert 'Content-Disposition' in response.headers
            assert 'inline; filename="narrative.pdf"' in response.headers['Content-Disposition']
    asyncio.run(run_test())


def test_special_provision_happy_path():
    """Happy path for special_provision doc_type."""
    async def run_test():
        pdf_content = b"special provision pdf"
        bucket = _FakeBucket(pdf_bytes=pdf_content)

        with patch("app.api.review.get_db") as mock_get_db, \
             patch("app.api.review.user_id_from_token") as mock_token:
            mock_token.return_value = "user-123"
            db_mock = _FakeDB(
                table_data={
                    "id": "proj-1",
                    "user_id": "user-123",
                    "special_provision_pdf_path": "user-123/proj-1/special_provision.pdf",
                },
                bucket=bucket,
            )
            mock_get_db.return_value = db_mock

            response = await review_pdf_endpoint("proj-1", "special_provision", "Bearer token")

            assert response.status_code == 200
            assert response.media_type == "application/pdf"
            assert response.body == pdf_content
            assert 'inline; filename="special_provision.pdf"' in response.headers['Content-Disposition']
    asyncio.run(run_test())


def test_key_map_happy_path():
    """Happy path for key_map doc_type."""
    async def run_test():
        pdf_content = b"key map pdf"
        bucket = _FakeBucket(pdf_bytes=pdf_content)

        with patch("app.api.review.get_db") as mock_get_db, \
             patch("app.api.review.user_id_from_token") as mock_token:
            mock_token.return_value = "user-123"
            db_mock = _FakeDB(
                table_data={
                    "id": "proj-1",
                    "user_id": "user-123",
                    "key_map_pdf_path": "user-123/proj-1/key_map.pdf",
                },
                bucket=bucket,
            )
            mock_get_db.return_value = db_mock

            response = await review_pdf_endpoint("proj-1", "key_map", "Bearer token")

            assert response.status_code == 200
            assert 'inline; filename="key_map.pdf"' in response.headers['Content-Disposition']
    asyncio.run(run_test())


def test_estimate_happy_path():
    """Happy path for estimate doc_type."""
    async def run_test():
        pdf_content = b"estimate pdf"
        bucket = _FakeBucket(pdf_bytes=pdf_content)

        with patch("app.api.review.get_db") as mock_get_db, \
             patch("app.api.review.user_id_from_token") as mock_token:
            mock_token.return_value = "user-123"
            db_mock = _FakeDB(
                table_data={
                    "id": "proj-1",
                    "user_id": "user-123",
                    "estimate_pdf_path": "user-123/proj-1/estimate.pdf",
                },
                bucket=bucket,
            )
            mock_get_db.return_value = db_mock

            response = await review_pdf_endpoint("proj-1", "estimate", "Bearer token")

            assert response.status_code == 200
            assert 'inline; filename="estimate.pdf"' in response.headers['Content-Disposition']
    asyncio.run(run_test())


def test_bucket_download_raises_returns_502():
    """If bucket.download() raises an exception, should return 502."""
    async def run_test():
        bucket = _FakeBucket(raise_error=Exception("Storage unavailable"))

        with patch("app.api.review.get_db") as mock_get_db, \
             patch("app.api.review.user_id_from_token") as mock_token:
            mock_token.return_value = "user-123"
            db_mock = _FakeDB(
                table_data={
                    "id": "proj-1",
                    "user_id": "user-123",
                    "narrative_pdf_path": "user-123/proj-1/narrative.pdf",
                },
                bucket=bucket,
            )
            mock_get_db.return_value = db_mock

            try:
                await review_pdf_endpoint("proj-1", "narrative", "Bearer token")
                assert False, "Should have raised HTTPException"
            except HTTPException as e:
                assert e.status_code == 502
                assert "Failed to fetch stored file" in e.detail
    asyncio.run(run_test())


if __name__ == "__main__":
    test_functions = [
        test_unknown_doc_type_returns_404,
        test_no_authorization_header_returns_401,
        test_project_not_found_returns_404,
        test_user_mismatch_returns_403,
        test_no_path_stored_returns_404,
        test_happy_path_returns_pdf_bytes,
        test_special_provision_happy_path,
        test_key_map_happy_path,
        test_estimate_happy_path,
        test_bucket_download_raises_returns_502,
    ]

    failures = 0
    for test_func in test_functions:
        try:
            test_func()
            print(f"PASS {test_func.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test_func.__name__}: {exc}")
            import traceback
            traceback.print_exc()

    total = len(test_functions)
    print(f"\n{total - failures}/{total} passed")
    sys.exit(1 if failures else 0)
