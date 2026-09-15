"""backend/tests/test_request_logging.py

Tests for app.request_logging.log_request_outcome -- the inbound middleware
that logs every non-2xx response and every unhandled exception.

Exercised against a throwaway FastAPI app rather than app.main:app, because
importing app.main executes app/api/query.py's module-level get_db() and
would require live Supabase credentials.

Runnable two ways:
    python tests/test_request_logging.py
    python -m pytest tests/test_request_logging.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.request_logging import log_request_outcome  # noqa: E402
from tests.logcapture import capture_logs  # noqa: E402

_LOGGER_NAME = "app.request"


def _client() -> TestClient:
    app = FastAPI()
    app.middleware("http")(log_request_outcome)

    @app.get("/ok")
    def ok():
        return {"ok": True}

    @app.get("/rejected")
    def rejected():
        raise HTTPException(status_code=400, detail="nope")

    @app.get("/upstream")
    def upstream():
        raise HTTPException(status_code=502, detail="storage unreachable")

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    @app.get("/secret")
    def secret():
        raise HTTPException(status_code=403, detail="no")

    # raise_server_exceptions=False so an unhandled error becomes a 500
    # response the test can assert on, instead of being re-raised into the
    # test body.
    return TestClient(app, raise_server_exceptions=False)


def test_success_is_not_logged():
    """This is an error log, not an access log -- uvicorn already prints one
    line per request, and duplicating that for every 200 buries the failures
    this plan exists to surface."""
    with capture_logs(_LOGGER_NAME, logging.DEBUG) as records:
        assert _client().get("/ok").status_code == 200
    assert records == []


def test_client_error_logs_a_warning():
    with capture_logs(_LOGGER_NAME) as records:
        assert _client().get("/rejected").status_code == 400
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    text = records[0].getMessage()
    assert "GET" in text
    assert "/rejected" in text
    assert "400" in text


def test_server_error_logs_an_error():
    with capture_logs(_LOGGER_NAME) as records:
        assert _client().get("/upstream").status_code == 502
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert "502" in records[0].getMessage()


def test_unhandled_exception_logs_an_error_with_traceback():
    with capture_logs(_LOGGER_NAME) as records:
        assert _client().get("/boom").status_code == 500
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert "unhandled exception" in records[0].getMessage()
    assert records[0].exc_info is not None  # traceback attached


def test_query_string_is_never_logged():
    """GET /api/review/{id}/status carries a Supabase JWT in ?token= --
    logging request.url instead of request.url.path would write that token
    into the server log on every failed stream."""
    with capture_logs(_LOGGER_NAME) as records:
        _client().get("/secret?token=super-secret-jwt&project=p1")
    assert len(records) == 1
    text = records[0].getMessage()
    assert "super-secret-jwt" not in text
    assert "token" not in text
    assert "/secret" in text


def test_elapsed_milliseconds_are_reported():
    with capture_logs(_LOGGER_NAME) as records:
        _client().get("/rejected")
    assert "ms" in records[0].getMessage()


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
    sys.exit(1 if failures else 0)
