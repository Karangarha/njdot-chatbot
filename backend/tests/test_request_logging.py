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

from app.request_logging import (  # noqa: E402
    _NOISY_DEPENDENCIES,
    _AccessLogHygiene,
    configure_console_logging,
    log_request_outcome,
)
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


# ── uvicorn access-log hygiene ────────────────────────────────────────────
#
# uvicorn packs its access record args as
# (client_addr, method, full_path, http_version, status_code) -- see
# uvicorn.logging.AccessFormatter.formatMessage. These tests use that exact
# shape, so they break loudly if uvicorn ever changes it.


def _access_record(full_path, status=200):
    return logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("10.0.0.1:0", "GET", full_path, "1.1", status),
        exc_info=None,
    )


def test_access_filter_drops_the_review_status_stream():
    """One SSE connection per review, held for its whole duration and
    reconnected on any blip. Its access line says nothing the review's own
    log lines do not say better."""
    assert _AccessLogHygiene().filter(_access_record("/api/review/abc-123/status")) is False


def test_access_filter_drops_the_session_status_stream():
    """Different URL shape -- the id is the LAST segment here, so a naive
    endswith('/status') check would miss it."""
    assert _AccessLogHygiene().filter(_access_record("/api/session/status/sess-9")) is False


def test_access_filter_redacts_the_supabase_jwt_from_the_query_string():
    """uvicorn logs get_path_with_query_string(scope), which appends the raw
    query string -- so a token passed there lands in the console verbatim.
    EventSource cannot set an Authorization header, which is why
    /api/review/{id}/status takes the token this way at all."""
    record = _access_record("/api/query?token=eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.secret")

    assert _AccessLogHygiene().filter(record) is True
    logged = record.getMessage()
    assert "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.secret" not in logged
    assert "<redacted>" in logged
    assert "/api/query" in logged      # the path itself is still useful


def test_access_filter_leaves_ordinary_requests_alone():
    record = _access_record("/api/conversations")
    assert _AccessLogHygiene().filter(record) is True
    assert record.args[2] == "/api/conversations"


def test_access_filter_passes_through_records_of_another_shape():
    """uvicorn.error records share the logger tree but not the args tuple.
    Anything not in the 5-tuple access shape must survive untouched."""
    record = logging.LogRecord(
        name="uvicorn.error", level=logging.INFO, pathname=__file__, lineno=1,
        msg="Application startup complete.", args=(), exc_info=None,
    )
    assert _AccessLogHygiene().filter(record) is True


# ── dependency noise ──────────────────────────────────────────────────────


def test_configure_console_logging_quiets_per_request_dependencies():
    """httpx logs one INFO line per outbound call. A single review makes 60+
    LLM calls plus every Supabase and Storage round trip, so at INFO the
    dependency chatter buries this app's own findings."""
    previous = {n: logging.getLogger(n).level for n in _NOISY_DEPENDENCIES}
    try:
        for name in _NOISY_DEPENDENCIES:
            logging.getLogger(name).setLevel(logging.NOTSET)
        configure_console_logging()

        for name in _NOISY_DEPENDENCIES:
            lg = logging.getLogger(name)
            assert lg.level == logging.WARNING, name
            assert not lg.isEnabledFor(logging.INFO), name
            # Not silenced -- a real problem still surfaces.
            assert lg.isEnabledFor(logging.WARNING), name
    finally:
        for name, lvl in previous.items():
            logging.getLogger(name).setLevel(lvl)


def test_configure_console_logging_is_idempotent():
    """main.py calls it once, but --reload re-imports; a second call must not
    stack duplicate filters and log every access line twice."""
    access = logging.getLogger("uvicorn.access")
    before = len([f for f in access.filters if isinstance(f, _AccessLogHygiene)])
    configure_console_logging()
    configure_console_logging()
    after = len([f for f in access.filters if isinstance(f, _AccessLogHygiene)])
    assert after == max(before, 1)


def test_app_loggers_are_not_quieted():
    """The dependency list must never swallow this application's own
    namespaces -- they are the whole point of the logging work."""
    for name in _NOISY_DEPENDENCIES:
        assert not name.startswith("app"), name


# ── main.py ordering invariants ───────────────────────────────────────────
#
# app.main cannot be imported by a test (it opens a Supabase client at import
# time and needs live credentials), so these two constraints are asserted
# against the source text instead. Both have already been broken once by an
# ordinary-looking edit, and neither fails loudly -- the first silently drops
# startup records, the second is a NameError only a real server start reveals.


def _main_source():
    return (_ROOT / "app" / "main.py").read_text(encoding="utf-8")


def test_logging_is_configured_before_the_routers_are_imported():
    """app/api/query.py calls get_db() at module-import time. If basicConfig
    has not run by then, the root logger is still an unconfigured WARNING
    with no handlers and every INFO record emitted during router import is
    dropped -- including 'Supabase client initialized'."""
    src = _main_source()
    assert src.index("logging.basicConfig(") < src.index("from app.api.auth import"), (
        "logging.basicConfig must stay ABOVE the app.api.* router imports"
    )


def test_request_logging_is_imported_before_it_is_called():
    """configure_console_logging() runs inside the logging block, which now
    sits above the main import group -- so its own import has to be hoisted
    with it. Sorting main.py's imports 'tidily' reintroduces a NameError at
    server start that no test here would otherwise catch."""
    src = _main_source()
    # Anchored to the call statement at column 0, so prose above the import
    # that merely names the function does not count as the call site.
    call = "\nconfigure_console_logging()"
    assert call in src, "configure_console_logging() is no longer called at module level in main.py"
    assert src.index("from app.request_logging import") < src.index(call), (
        "the app.request_logging import must stay ABOVE the configure_console_logging() call"
    )


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
