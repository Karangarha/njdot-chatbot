"""backend/tests/test_session_auth.py

Sign-in + ownership on /api/session/* (sandbox finding 2). Mocks Supabase
and token decoding; no network.

Runnable two ways:
    python backend/tests/test_session_auth.py
    python -m pytest backend/tests/test_session_auth.py
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

from app import project_access  # noqa: E402
from app.api.review import _review_progress  # noqa: E402
from app.api.session import QueryRequest, get_session_messages, session_query  # noqa: E402

OWNER, OTHER = "owner-uuid", "other-uuid"
PID = "11111111-1111-1111-1111-111111111111"


class _FakeQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return type("Executed", (), {"data": self._data})()


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _FakeQuery(self._rows)


class _DownDB:
    def table(self, name):
        raise ConnectionError("supabase unreachable")


def _status(fn, *args, **kwargs):
    """Run fn; return the HTTPException status it raised, or None."""
    try:
        result = fn(*args, **kwargs)
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    except HTTPException as exc:
        return exc.status_code
    return None


def setup_function(_fn=None):
    _review_progress.pop(PID, None)


def test_no_token_is_401_even_for_unknown_project():
    with patch("app.project_access.get_db", return_value=_FakeDB([])):
        assert _status(project_access.require_project_access, PID, None) == 401


def test_invalid_token_is_401():
    with patch("app.project_access.get_db", return_value=_FakeDB([])):
        assert _status(project_access.require_project_access, PID, "Bearer not-a-jwt") == 401


def test_other_users_project_is_403():
    with patch("app.project_access.get_db", return_value=_FakeDB([{"user_id": OWNER}])), \
         patch("app.project_access.user_id_from_token", return_value=OTHER):
        assert _status(project_access.require_project_access, PID, "Bearer x") == 403


def test_owner_is_allowed_from_db():
    with patch("app.project_access.get_db", return_value=_FakeDB([{"user_id": OWNER}])), \
         patch("app.project_access.user_id_from_token", return_value=OWNER):
        assert project_access.require_project_access(PID, "Bearer x") == OWNER


def test_owner_is_allowed_from_in_process_store_without_db():
    # A review that just finished isn't in review_projects yet.
    _review_progress[PID] = {"status": "ready", "user_id": OWNER}
    with patch("app.project_access.get_db", return_value=_DownDB()), \
         patch("app.project_access.user_id_from_token", return_value=OWNER):
        assert project_access.require_project_access(PID, "Bearer x") == OWNER


def test_ownerless_project_allowed_for_signed_in_user():
    with patch("app.project_access.get_db", return_value=_FakeDB([])), \
         patch("app.project_access.user_id_from_token", return_value=OTHER):
        assert project_access.require_project_access(PID, "Bearer x") == OTHER


def test_standalone_upload_skips_owner_lookup():
    # project_id="" must not query review_projects (uuid column rejects '').
    with patch("app.project_access.get_db", return_value=_DownDB()), \
         patch("app.project_access.user_id_from_token", return_value=OTHER):
        assert project_access.require_project_access("", "Bearer x") == OTHER


def test_db_down_fails_closed():
    with patch("app.project_access.get_db", return_value=_DownDB()), \
         patch("app.project_access.user_id_from_token", return_value=OWNER):
        assert _status(project_access.require_project_access, PID, "Bearer x") == 503


def test_messages_endpoint_requires_token():
    with patch("app.project_access.get_db", return_value=_FakeDB([])):
        assert _status(get_session_messages, PID, authorization=None) == 401


def test_query_endpoint_requires_token():
    req = QueryRequest(question="hi", session_id=PID)
    with patch("app.project_access.get_db", return_value=_FakeDB([])):
        assert _status(session_query, req, authorization=None) == 401


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
