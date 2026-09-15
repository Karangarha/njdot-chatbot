# API Error and Warning Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every API failure visible in the backend console — both inbound (a request this app rejected or crashed on) and outbound (an OpenAI/Anthropic/Supabase/Neo4j/httpx call that failed) — as a consistent ERROR or WARNING log line, instead of the silent `except: pass` / `return None` fallbacks the code relies on today.

**Architecture:** Two pieces of shared machinery plus targeted log lines at the sites that currently swallow failures. Inbound is one `@app.middleware("http")` dispatch function that logs any non-2xx response and any unhandled exception — one place, covers every route now and later. Outbound LLM calls are covered by one `BaseCallbackHandler` subclass wired into the callback list every LangChain `.invoke()` already builds, so a provider failure or an OpenAI→Anthropic fallback logs itself without touching 30 call sites. Everything else (Supabase, Neo4j, raw httpx, PDF parsing, review gating) gets an explicit `logger.warning`/`logger.error` at the exact line that currently discards the error.

**Tech Stack:** Python 3.11+, stdlib `logging`, FastAPI/Starlette middleware, `langchain_core.callbacks.BaseCallbackHandler` (langchain-core 1.4.9), pytest 9.1.1 + `fastapi.testclient.TestClient`.

## Global Constraints

- **Server-side console logging only.** No change to any HTTP response body, status code, or the SSE event shape. No frontend changes.
- **Level policy, applied everywhere in this plan:**
  - `ERROR` — the operation could not complete as intended (5xx, unhandled exception, an LLM call that exhausted its fallbacks, a Storage download that failed).
  - `WARNING` — something failed but the code deliberately continued in a degraded state (4xx rejection, provider fallback fired, an extraction returned nothing, a table was skipped, a check downgraded to Missing for lack of evidence).
  - `INFO` and below — not touched by this plan.
- **Never log a URL query string.** `GET /api/review/{project_id}/status?token=<JWT>` carries a Supabase access token in the query string ([review.py:191](../../../backend/app/api/review.py:191)). Log `request.url.path`, never `request.url`.
- **Never log document bytes, chunk text, embeddings, passwords, or tokens.** Log identifiers (`project_id`, `session_id`, `check_key`, `taskId`, path, status code) and exception text only.
- **Every new log call uses `%`-style lazy formatting** (`logger.warning("x=%s", x)`), never an f-string — matches every existing call site in this codebase.
- **`exc_info=True` on ERROR only.** A WARNING for a deliberate degraded path gets the exception's `str()` in the message, not a full traceback — these fire often and a traceback each time drowns the log.
- **Run all Python commands from the repo root using `.venv/Scripts/python.exe`.** `backend/.venv` is a stray venv without pytest — do not use it.
- **`backend/.gitignore` starts with a blanket `*`.** New files under `backend/app/` and `backend/tests/` are already un-ignored by the existing `!app/*.py` and `!tests/*` rules, so no `.gitignore` edit is needed for the files this plan creates. Verify with `git status --short` before each commit that the new file actually appears.

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `backend/app/request_logging.py` | The inbound middleware dispatch function. Separate from `main.py` so it can be tested against a throwaway FastAPI app — importing `app.main` at test time would execute `app/api/query.py`'s module-level `get_db()` and require live Supabase credentials. |
| `backend/app/llm_logging.py` | `LLMLoggingCallbackHandler` (LangChain errors/retries → logs) and `build_callbacks()`, the single source of the callback list every LLM `.invoke()` should pass. |
| `backend/tests/logcapture.py` | `capture_logs(...)` context manager used by every test in this plan. Pure stdlib so the repo's existing "runnable as a plain script" test convention keeps working (pytest's `caplog` fixture would break it). |
| `backend/tests/test_request_logging.py` | Tests for Task 1. |
| `backend/tests/test_llm_logging.py` | Tests for Tasks 2 and 3. |
| `backend/tests/test_outbound_logging.py` | Tests for Task 4. |
| `backend/tests/test_ingestion_logging.py` | Tests for Task 5. |
| `backend/tests/test_review_degraded_logging.py` | Tests for Task 6. |

**Modified:** `backend/app/main.py`, `backend/app/database.py`, `backend/app/neo4j_client.py`, `backend/app/auth.py`, `backend/app/api/auth.py`, `backend/app/api/pdf.py`, `backend/app/api/review.py`, `backend/app/api/session.py`, `backend/app/compliance/eval_engine.py`, `backend/app/compliance/edq.py`, `backend/app/ingestion/keymap_extractor.py`, `backend/app/ingestion/estimate_extractor.py`, `backend/app/ingestion/edq_extractor.py`, `backend/app/ingestion/utility_plan_extractor.py`, `backend/app/ingestion/table_extractor.py`, `backend/app/ingestion/session_chunker.py`, `backend/tests/test_eval_engine.py`.

## Deliberately out of scope

Not built here; each would be its own plan if wanted later:

- A request/correlation id. `project_id` and `session_id` already thread through the two long-running flows, which is where correlation actually matters.
- A JSON log formatter or a structured-logging dependency (`structlog`, `python-json-logger`). The existing `logging.basicConfig` format stays.
- Log levels per module, or any new environment variable.
- Client-facing error codes or a `warnings[]` field on responses.
- Frontend `console.error`/`console.warn` in `api.ts`, `ChatInterface.tsx`, `DocumentReview.tsx`, `SessionChat.tsx` — these swallow plenty too, but the user scoped this plan to the backend.

---

### Task 1: Inbound request logging middleware

**Files:**
- Create: `backend/app/request_logging.py`
- Create: `backend/tests/logcapture.py`
- Create: `backend/tests/test_request_logging.py`
- Modify: `backend/app/main.py:66-82`

**Interfaces:**
- Produces: `log_request_outcome(request: Request, call_next: Callable) -> Response` in `app.request_logging` — an async Starlette middleware dispatch function, registered via `app.middleware("http")(log_request_outcome)`.
- Produces: `capture_logs(logger_name: str, level: int = logging.WARNING)` in `tests.logcapture` — a context manager yielding a `list[logging.LogRecord]` that every later task's tests consume.

- [ ] **Step 1: Write the test-helper module**

Create `backend/tests/logcapture.py`:

```python
"""backend/tests/logcapture.py

Stdlib log capture for the backend test suite.

Deliberately not pytest's ``caplog`` fixture: every test file in this
directory is runnable both under pytest AND as a plain script (see each
file's ``if __name__ == "__main__":`` runner), and a fixture parameter
breaks the plain-script path.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, List


class _ListHandler(logging.Handler):
    """Collects every emitted record instead of writing it anywhere."""

    def __init__(self) -> None:
        super().__init__()
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_logs(logger_name: str, level: int = logging.WARNING) -> Iterator[List[logging.LogRecord]]:
    """Capture records emitted by ``logger_name`` at ``level`` or above.

    Attaches to the named logger directly (not the root logger), so a test
    asserting on one module's output is unaffected by anything else that
    happens to log during the same test.
    """
    handler = _ListHandler()
    target = logging.getLogger(logger_name)
    previous_level = target.level
    target.addHandler(handler)
    target.setLevel(level)
    try:
        yield handler.records
    finally:
        target.removeHandler(handler)
        target.setLevel(previous_level)
```

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/test_request_logging.py`:

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_request_logging.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'app.request_logging'`.

- [ ] **Step 4: Write the middleware**

Create `backend/app/request_logging.py`:

```python
"""Inbound request outcome logging.

One Starlette middleware dispatch function, registered once in
``app.main``, that logs every request this app rejected (4xx) or broke on
(5xx / unhandled exception). Covers every route in ``app.api`` — present
and future — without each handler having to remember to log.

Lives in its own module rather than inline in ``main.py`` so tests can
import it against a throwaway FastAPI app: importing ``app.main`` executes
``app.api.query``'s module-level ``get_db()`` call and would require live
Supabase credentials just to test a log line.

Two things this deliberately does NOT do:

* **Log successful requests.** uvicorn already prints one access line per
  request; a second copy for every 200 buries the failures.
* **Log the query string.** ``GET /api/review/{project_id}/status`` passes
  the caller's Supabase JWT as ``?token=`` (EventSource cannot set an
  Authorization header — see ``app.api.review.review_status``), so only
  ``request.url.path`` is ever logged.

Not covered here, by design: exceptions raised inside a
``StreamingResponse`` generator (the SSE endpoints) surface after this
middleware has already returned the response, and work scheduled on
``BackgroundTasks`` runs after the response is sent. Both already log their
own failures — see ``app.api.review._run_review_pipeline_background``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

logger = logging.getLogger("app.request")


async def log_request_outcome(request: Any, call_next: Callable) -> Any:
    """Log the outcome of one request. See the module docstring for scope."""
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.error(
            "%s %s -> unhandled exception after %.0fms",
            request.method, request.url.path, elapsed_ms, exc_info=True,
        )
        raise

    elapsed_ms = (time.perf_counter() - start) * 1000
    if response.status_code >= 500:
        logger.error(
            "%s %s -> %d after %.0fms",
            request.method, request.url.path, response.status_code, elapsed_ms,
        )
    elif response.status_code >= 400:
        logger.warning(
            "%s %s -> %d after %.0fms",
            request.method, request.url.path, response.status_code, elapsed_ms,
        )
    return response
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_request_logging.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Register the middleware in `main.py`**

In `backend/app/main.py`, find the imports block and add the new import next to the existing router imports:

```python
from app.api.auth import router as auth_router
from app.api.conversations import router as conversations_router
from app.api.pdf import router as pdf_router
from app.api.query import router as query_router
from app.api.review import router as review_router
from app.api.session import router as session_router
from app.config import config
```

Change to:

```python
from app.api.auth import router as auth_router
from app.api.conversations import router as conversations_router
from app.api.pdf import router as pdf_router
from app.api.query import router as query_router
from app.api.review import router as review_router
from app.api.session import router as session_router
from app.config import config
from app.request_logging import log_request_outcome
```

Then find the CORS block:

```python
# ── CORS ──────────────────────────────────────────────────────────────────────
_allowed_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://cm-chatbot-coral.vercel.app",
    "https://cm-smart-assistant.vercel.app",
]

if config.FRONTEND_URL and config.FRONTEND_URL not in _allowed_origins:
    _allowed_origins.append(config.FRONTEND_URL)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

Change to (the logging middleware is registered **above** the CORS block — read the comment, the order is load-bearing):

```python
# ── Request outcome logging ───────────────────────────────────────────────────
# Registered BEFORE CORSMiddleware below, and the order matters: Starlette
# inserts each newly-added middleware at the OUTSIDE of the stack, so
# whatever is added last ends up outermost. Adding this one first leaves
# CORSMiddleware outermost, which is required because log_request_outcome
# re-raises an unhandled exception — if it sat outside CORS, that exception
# would bypass CORSMiddleware entirely and the resulting 500 would carry no
# CORS headers, surfacing in the browser as a misleading "blocked by CORS
# policy" error instead of the real server error.
app.middleware("http")(log_request_outcome)

# ── CORS ──────────────────────────────────────────────────────────────────────
_allowed_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://cm-chatbot-coral.vercel.app",
    "https://cm-smart-assistant.vercel.app",
]

if config.FRONTEND_URL and config.FRONTEND_URL not in _allowed_origins:
    _allowed_origins.append(config.FRONTEND_URL)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

- [ ] **Step 7: Verify `main.py` still imports cleanly**

Run: `.venv/Scripts/python.exe -c "import sys; sys.path.insert(0, 'backend'); import app.request_logging; print('request_logging OK')"`
Expected: prints `request_logging OK`.

`app.main` itself is not import-checked here — it opens a Supabase client at import time and needs live credentials, which this task does not require.

- [ ] **Step 8: Run the full backend suite**

Run: `.venv/Scripts/python.exe -m pytest backend/tests -q`
Expected: all tests pass. Nothing existing imports `app.main` or `app.request_logging`, so this is a confirmation, not an expected source of change.

- [ ] **Step 9: Commit**

```bash
git add backend/app/request_logging.py backend/app/main.py backend/tests/logcapture.py backend/tests/test_request_logging.py
git commit -m "feat: log every rejected and failed inbound request"
```

---

### Task 2: LangChain callback logging for outbound LLM calls

**Files:**
- Create: `backend/app/llm_logging.py`
- Create: `backend/tests/test_llm_logging.py`

**Interfaces:**
- Consumes: `tests.logcapture.capture_logs` (Task 1).
- Consumes: `app.observability.get_langfuse_handler(trace_id=None, parent_span_id=None)` — existing, returns a handler or `None`.
- Produces: `LLMLoggingCallbackHandler(operation: Optional[str] = None)` in `app.llm_logging` — a `BaseCallbackHandler` subclass implementing `on_llm_error`, `on_retry`, `on_tool_error`, `on_retriever_error`, `on_chain_error`.
- Produces: `build_callbacks(trace_id: Optional[str] = None, parent_span_id: Optional[str] = None, operation: Optional[str] = None) -> list` in `app.llm_logging` — the callback list every LLM `.invoke()` should pass. Task 3 wires this into all seven call sites.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_llm_logging.py`:

```python
"""backend/tests/test_llm_logging.py

Tests for app.llm_logging -- the LangChain callback handler that turns an
LLM/tool/retriever failure into a console log line, and build_callbacks(),
the single source of the callback list every .invoke() passes.

No network, no real LLM: the handler's callback methods are invoked
directly, exactly as LangChain's callback manager would invoke them.

Runnable two ways:
    python tests/test_llm_logging.py
    python -m pytest tests/test_llm_logging.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.llm_logging import LLMLoggingCallbackHandler, build_callbacks  # noqa: E402
from tests.logcapture import capture_logs  # noqa: E402

_LOGGER_NAME = "app.llm"


def test_llm_error_logs_an_error_with_traceback():
    handler = LLMLoggingCallbackHandler(operation="evaluate-check:no_lag")
    with capture_logs(_LOGGER_NAME) as records:
        handler.on_llm_error(RuntimeError("429 rate limit"), run_id=uuid4())

    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    text = records[0].getMessage()
    assert "evaluate-check:no_lag" in text
    assert "429 rate limit" in text
    assert records[0].exc_info is not None


def test_llm_error_without_operation_still_logs():
    """operation is optional -- a call site that hasn't named its operation
    must still produce a usable line, not crash on a None format arg."""
    handler = LLMLoggingCallbackHandler()
    with capture_logs(_LOGGER_NAME) as records:
        handler.on_llm_error(ValueError("bad request"), run_id=uuid4())

    assert len(records) == 1
    assert "bad request" in records[0].getMessage()


def test_retry_logs_a_warning():
    """A retry means the call failed once but the run continues -- WARNING,
    not ERROR, per this plan's level policy. This is also the only signal
    that OpenAI is degrading before .with_fallbacks() switches to Claude."""

    class _RetryState:
        outcome = None
        attempt_number = 2

        def __init__(self):
            class _Outcome:
                @staticmethod
                def exception():
                    return TimeoutError("read timeout")
            self.outcome = _Outcome()

    handler = LLMLoggingCallbackHandler(operation="extract-key-map")
    with capture_logs(_LOGGER_NAME) as records:
        handler.on_retry(_RetryState(), run_id=uuid4())

    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    text = records[0].getMessage()
    assert "extract-key-map" in text
    assert "read timeout" in text
    assert records[0].exc_info is None  # WARNING carries no traceback


def test_retry_with_no_outcome_does_not_crash():
    """tenacity's RetryCallState can carry outcome=None on the first call."""

    class _RetryState:
        outcome = None
        attempt_number = 1

    handler = LLMLoggingCallbackHandler()
    with capture_logs(_LOGGER_NAME) as records:
        handler.on_retry(_RetryState(), run_id=uuid4())

    assert len(records) == 1
    assert records[0].levelno == logging.WARNING


def test_tool_and_retriever_and_chain_errors_log():
    handler = LLMLoggingCallbackHandler(operation="chat-agent-response")
    with capture_logs(_LOGGER_NAME) as records:
        handler.on_tool_error(RuntimeError("cypher exploded"), run_id=uuid4())
        handler.on_retriever_error(RuntimeError("pgvector down"), run_id=uuid4())
        handler.on_chain_error(RuntimeError("chain broke"), run_id=uuid4())

    assert len(records) == 3
    assert all(r.levelno == logging.ERROR for r in records)
    joined = " ".join(r.getMessage() for r in records)
    assert "cypher exploded" in joined
    assert "pgvector down" in joined
    assert "chain broke" in joined


def test_build_callbacks_always_includes_the_logging_handler():
    with patch("app.llm_logging.get_langfuse_handler", return_value=None):
        callbacks = build_callbacks(operation="extract-estimate")

    assert len(callbacks) == 1
    assert isinstance(callbacks[0], LLMLoggingCallbackHandler)


def test_build_callbacks_appends_langfuse_when_available():
    sentinel = object()
    with patch("app.llm_logging.get_langfuse_handler", return_value=sentinel):
        callbacks = build_callbacks(trace_id="t1", parent_span_id="s1")

    assert len(callbacks) == 2
    assert isinstance(callbacks[0], LLMLoggingCallbackHandler)
    assert callbacks[1] is sentinel


def test_build_callbacks_passes_trace_context_to_langfuse():
    with patch("app.llm_logging.get_langfuse_handler", return_value=None) as mock_lf:
        build_callbacks(trace_id="trace-1", parent_span_id="span-1")

    mock_lf.assert_called_once_with(trace_id="trace-1", parent_span_id="span-1")


def test_build_callbacks_survives_a_broken_langfuse():
    """Langfuse being misconfigured must never cost us the error logging --
    that is exactly when the logs matter most."""
    with patch("app.llm_logging.get_langfuse_handler", side_effect=RuntimeError("langfuse down")):
        callbacks = build_callbacks()

    assert len(callbacks) == 1
    assert isinstance(callbacks[0], LLMLoggingCallbackHandler)


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_llm_logging.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'app.llm_logging'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/llm_logging.py`:

```python
"""Console logging for outbound LangChain calls.

Every LLM, tool and retriever call in this app goes through LangChain's
callback system, so one ``BaseCallbackHandler`` attached to that system
covers all of them — the ~60 structured-output calls a single compliance
review makes, the chat agent's tool calls, and the five vision extractions
— without a try/except at each site.

Two failures this makes visible that nothing else does:

* **Provider fallback.** ``ChatOpenAI(...).with_fallbacks([ChatAnthropic(...)])``
  (``app.api.review``, ``app.api.session``) silently switches to Claude when
  OpenAI errors. ``on_llm_error`` fires for the failed OpenAI run, so the
  switch stops being invisible.
* **Retries before the fallback.** ``on_retry`` fires per attempt, which is
  the earliest signal that a provider is degrading.

``build_callbacks`` exists so no call site ever constructs its callback
list by hand — a site that did would silently opt out of all of the above.
It is the only function the rest of the app should call.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from langchain_core.callbacks import BaseCallbackHandler

from app.observability import get_langfuse_handler

logger = logging.getLogger("app.llm")


class LLMLoggingCallbackHandler(BaseCallbackHandler):
    """Logs LangChain error and retry events to the console.

    ``operation`` is the same human-readable label the call site already
    passes as LangChain's ``run_name`` (e.g. ``"evaluate-check:no_lag"``,
    ``"extract-key-map"``), so a log line can be matched to the code that
    produced it. Optional — a site without one still logs usefully.

    Every method swallows nothing and raises nothing: a callback that
    raised would propagate into the caller's ``.invoke()`` and turn an
    observability concern into an outage.
    """

    def __init__(self, operation: Optional[str] = None) -> None:
        super().__init__()
        self.operation = operation or "llm call"

    # ── Errors: the call could not complete -> ERROR + traceback ──────────

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        logger.error("LLM call failed [%s]: %s", self.operation, error, exc_info=error)

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        logger.error("Tool call failed [%s]: %s", self.operation, error, exc_info=error)

    def on_retriever_error(self, error: BaseException, **kwargs: Any) -> None:
        logger.error("Retriever call failed [%s]: %s", self.operation, error, exc_info=error)

    def on_chain_error(self, error: BaseException, **kwargs: Any) -> None:
        logger.error("Chain failed [%s]: %s", self.operation, error, exc_info=error)

    # ── Retry: failed once, still running -> WARNING, no traceback ────────

    def on_retry(self, retry_state: Any, **kwargs: Any) -> None:
        outcome = getattr(retry_state, "outcome", None)
        error = outcome.exception() if outcome is not None else None
        logger.warning(
            "LLM call retrying [%s], attempt %s: %s",
            self.operation, getattr(retry_state, "attempt_number", "?"), error,
        )


def build_callbacks(
    trace_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
    operation: Optional[str] = None,
) -> List[Any]:
    """The callback list for one LangChain ``.invoke()``.

    Always contains ``LLMLoggingCallbackHandler``; appends the Langfuse
    handler when Langfuse is configured and reachable. Langfuse failing to
    construct must never cost us the error logging — that is precisely when
    the logs matter — so its construction is guarded separately here even
    though ``get_langfuse_handler`` already fails soft on its own.
    """
    callbacks: List[Any] = [LLMLoggingCallbackHandler(operation=operation)]
    try:
        langfuse_handler = get_langfuse_handler(
            trace_id=trace_id, parent_span_id=parent_span_id,
        )
    except Exception:
        logger.warning("Langfuse callback unavailable — continuing with logging only", exc_info=True)
        langfuse_handler = None
    if langfuse_handler is not None:
        callbacks.append(langfuse_handler)
    return callbacks
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_llm_logging.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/llm_logging.py backend/tests/test_llm_logging.py
git commit -m "feat: log LangChain LLM, tool and retry failures to the console"
```

---

### Task 3: Wire `build_callbacks` into every LLM call site

**Files:**
- Modify: `backend/app/compliance/eval_engine.py:57` (import), `:456` (`_evaluate_one_check` signature), `:527` (invoke_config), `:705-715` (`evaluate_checks`)
- Modify: `backend/app/compliance/edq.py:37` (import), `:173-175`
- Modify: `backend/app/api/session.py:63` (import), `:827-829`
- Modify: `backend/app/ingestion/keymap_extractor.py:29` (import), `:184-186`
- Modify: `backend/app/ingestion/estimate_extractor.py:30` (import), `:208-210`
- Modify: `backend/app/ingestion/edq_extractor.py:35` (import), `:135-137`, `:175-177`
- Modify: `backend/app/ingestion/utility_plan_extractor.py:24` (import), `:92-94`
- Modify: `backend/tests/test_eval_engine.py` (the `_call_evaluate_one_check` helper)
- Modify: `backend/tests/test_llm_logging.py` (append one test)

**Interfaces:**
- Consumes: `build_callbacks(trace_id=..., parent_span_id=..., operation=...)` from Task 2.
- Produces: `_evaluate_one_check`'s last parameter is renamed from `langfuse_handler` to `callbacks: List[Any]` — a list, not a single handler. `evaluate_checks`'s public signature is unchanged.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_llm_logging.py`, before the `if __name__ == "__main__":` block:

```python
# Every module that invokes an LLM. A site missing from build_callbacks()
# silently opts out of error logging for every call it makes, and nothing
# at runtime would notice -- so this is checked against the source text.
_CALLBACK_SITE_MODULES = [
    "app/compliance/eval_engine.py",
    "app/compliance/edq.py",
    "app/api/session.py",
    "app/ingestion/keymap_extractor.py",
    "app/ingestion/estimate_extractor.py",
    "app/ingestion/edq_extractor.py",
    "app/ingestion/utility_plan_extractor.py",
]


def test_every_llm_call_site_uses_build_callbacks():
    for relative_path in _CALLBACK_SITE_MODULES:
        source = (_ROOT / relative_path).read_text(encoding="utf-8")
        assert "build_callbacks(" in source, f"{relative_path} never calls build_callbacks()"
        assert "get_langfuse_handler(" not in source, (
            f"{relative_path} still builds a Langfuse-only callback list — "
            "its LLM failures will not be logged"
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_llm_logging.py::test_every_llm_call_site_uses_build_callbacks -v`
Expected: FAIL with `app/compliance/eval_engine.py never calls build_callbacks()`.

- [ ] **Step 3: Wire `eval_engine.py`**

Find the import on line 57 of `backend/app/compliance/eval_engine.py`:

```python
from app.observability import get_langfuse_client, get_langfuse_handler, new_trace_id
```

Change to:

```python
from app.llm_logging import build_callbacks
from app.observability import get_langfuse_client, new_trace_id
```

Find `_evaluate_one_check`'s signature (the last parameter, around line 455):

```python
    deterministic: "_DeterministicContext",
    project_id: str,
    user_id: Optional[str],
    langfuse_handler,
) -> Tuple[ReviewCheckResult, Dict[str, int]]:
```

Change to:

```python
    deterministic: "_DeterministicContext",
    project_id: str,
    user_id: Optional[str],
    callbacks: List[Any],
) -> Tuple[ReviewCheckResult, Dict[str, int]]:
```

Find the `invoke_config` construction (around line 526):

```python
    invoke_config = {
        "callbacks": [langfuse_handler] if langfuse_handler else [],
        "run_name": f"evaluate-check:{check.check_key}",
```

Change to:

```python
    invoke_config = {
        "callbacks": callbacks,
        "run_name": f"evaluate-check:{check.check_key}",
```

Find where the handler is built inside `evaluate_checks` (around line 704):

```python
    with review_span_cm as review_span:
        langfuse_handler = get_langfuse_handler(
            trace_id=trace_id, parent_span_id=getattr(review_span, "id", None),
        )
```

Change to:

```python
    with review_span_cm as review_span:
        callbacks = build_callbacks(
            trace_id=trace_id, parent_span_id=getattr(review_span, "id", None),
            operation="compliance-review",
        )
```

Find the `executor.submit` call (around line 710):

```python
                executor.submit(
                    _evaluate_one_check, check, structured_llm, structured_judge_llm,
                    schedule_facts, narrative_text,
                    sp_search_fn, spec_search_fn, csm_search_fn, keymap_facts, estimate_facts,
                    utility_plan_search_fn,
                    deterministic_ctx, project_id, user_id, langfuse_handler,
                ): i
```

Change to:

```python
                executor.submit(
                    _evaluate_one_check, check, structured_llm, structured_judge_llm,
                    schedule_facts, narrative_text,
                    sp_search_fn, spec_search_fn, csm_search_fn, keymap_facts, estimate_facts,
                    utility_plan_search_fn,
                    deterministic_ctx, project_id, user_id, callbacks,
                ): i
```

Confirm `Any` is importable in this module — line 44 currently reads:

```python
from typing import Callable, Dict, List, Optional, Tuple
```

Change to:

```python
from typing import Any, Callable, Dict, List, Optional, Tuple
```

- [ ] **Step 4: Update `test_eval_engine.py`'s helper for the renamed parameter**

In `backend/tests/test_eval_engine.py`, find `_call_evaluate_one_check`:

```python
        deterministic=_DeterministicContext(),
        project_id="default",
        user_id=None,
        langfuse_handler=None,
    )
```

Change to:

```python
        deterministic=_DeterministicContext(),
        project_id="default",
        user_id=None,
        callbacks=[],
    )
```

- [ ] **Step 5: Wire `edq.py`**

Find the import on line 37 of `backend/app/compliance/edq.py`:

```python
from app.observability import get_langfuse_handler, new_trace_id
```

Change to:

```python
from app.llm_logging import build_callbacks
from app.observability import new_trace_id
```

Find (around line 173):

```python
        handler = get_langfuse_handler(trace_id=new_trace_id(seed=project_id)) if project_id else None
        invoke_config = {
            "callbacks": [handler] if handler else [],
            "run_name": "match-edq-items",
```

Change to:

```python
        invoke_config = {
            "callbacks": build_callbacks(
                trace_id=new_trace_id(seed=project_id) if project_id else None,
                operation="match-edq-items",
            ),
            "run_name": "match-edq-items",
```

- [ ] **Step 6: Wire `session.py`**

Find the import on line 63 of `backend/app/api/session.py`:

```python
from app.observability import get_langfuse_handler
```

Change to:

```python
from app.llm_logging import build_callbacks
```

Find (around line 827):

```python
    langfuse_handler = get_langfuse_handler()
    invoke_config = {
        "callbacks": [langfuse_handler] if langfuse_handler else [],
        # Verb-first, low-cardinality name (no session_id/question text) so
```

Change to:

```python
    _run_name = "chat-agent-response" if has_graph else "chat-sp-response"
    invoke_config = {
        "callbacks": build_callbacks(operation=_run_name),
        # Verb-first, low-cardinality name (no session_id/question text) so
```

Then, a few lines below, find:

```python
        "run_name": "chat-agent-response" if has_graph else "chat-sp-response",
```

Change to:

```python
        "run_name": _run_name,
```

- [ ] **Step 7: Wire `keymap_extractor.py`**

Find the import on line 29 of `backend/app/ingestion/keymap_extractor.py`:

```python
from app.observability import get_langfuse_handler, new_trace_id
```

Change to:

```python
from app.llm_logging import build_callbacks
from app.observability import new_trace_id
```

Find (around line 184):

```python
        handler = get_langfuse_handler(trace_id=new_trace_id(seed=project_id)) if project_id else None
        invoke_config = {
            "callbacks": [handler] if handler else [],
            "run_name": "extract-key-map",
```

Change to:

```python
        invoke_config = {
            "callbacks": build_callbacks(
                trace_id=new_trace_id(seed=project_id) if project_id else None,
                operation="extract-key-map",
            ),
            "run_name": "extract-key-map",
```

- [ ] **Step 8: Wire `estimate_extractor.py`**

Find the import on line 30 of `backend/app/ingestion/estimate_extractor.py`:

```python
from app.observability import get_langfuse_handler, new_trace_id
```

Change to:

```python
from app.llm_logging import build_callbacks
from app.observability import new_trace_id
```

Find (around line 208):

```python
        handler = get_langfuse_handler(trace_id=new_trace_id(seed=project_id)) if project_id else None
        invoke_config = {
            "callbacks": [handler] if handler else [],
            "run_name": "extract-estimate",
```

Change to:

```python
        invoke_config = {
            "callbacks": build_callbacks(
                trace_id=new_trace_id(seed=project_id) if project_id else None,
                operation="extract-estimate",
            ),
            "run_name": "extract-estimate",
```

- [ ] **Step 9: Wire `edq_extractor.py` (two sites)**

Find the import on line 35 of `backend/app/ingestion/edq_extractor.py`:

```python
from app.observability import get_langfuse_handler, new_trace_id
```

Change to:

```python
from app.llm_logging import build_callbacks
from app.observability import new_trace_id
```

Find the first site (around line 135, inside `locate_edq_section_pages`):

```python
        handler = get_langfuse_handler(trace_id=new_trace_id(seed=project_id)) if project_id else None
        invoke_config = {
            "callbacks": [handler] if handler else [],
            "run_name": "locate-edq-section",
```

Change to:

```python
        invoke_config = {
            "callbacks": build_callbacks(
                trace_id=new_trace_id(seed=project_id) if project_id else None,
                operation="locate-edq-section",
            ),
            "run_name": "locate-edq-section",
```

Find the second site (around line 175, inside `_extract_page`):

```python
        handler = get_langfuse_handler(trace_id=new_trace_id(seed=project_id)) if project_id else None
        invoke_config = {
            "callbacks": [handler] if handler else [],
            "run_name": "extract-edq-page",
```

Change to:

```python
        invoke_config = {
            "callbacks": build_callbacks(
                trace_id=new_trace_id(seed=project_id) if project_id else None,
                operation="extract-edq-page",
            ),
            "run_name": "extract-edq-page",
```

- [ ] **Step 10: Wire `utility_plan_extractor.py`**

Find the import on line 24 of `backend/app/ingestion/utility_plan_extractor.py`:

```python
from app.observability import get_langfuse_handler, new_trace_id
```

Change to:

```python
from app.llm_logging import build_callbacks
from app.observability import new_trace_id
```

Find (around line 92):

```python
        handler = get_langfuse_handler(trace_id=new_trace_id(seed=project_id)) if project_id else None
        invoke_config = {
            "callbacks": [handler] if handler else [],
            "run_name": "extract-utility-plan",
```

Change to:

```python
        invoke_config = {
            "callbacks": build_callbacks(
                trace_id=new_trace_id(seed=project_id) if project_id else None,
                operation="extract-utility-plan",
            ),
            "run_name": "extract-utility-plan",
```

- [ ] **Step 11: Run the wiring test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_llm_logging.py -v`
Expected: PASS (10 tests)

- [ ] **Step 12: Run the full backend suite to check for regressions**

Run: `.venv/Scripts/python.exe -m pytest backend/tests -q`
Expected: all tests pass. `test_eval_engine.py` is the only existing file that names the renamed parameter, and Step 4 updated it.

- [ ] **Step 13: Commit**

```bash
git add backend/app/compliance/eval_engine.py backend/app/compliance/edq.py backend/app/api/session.py backend/app/ingestion/keymap_extractor.py backend/app/ingestion/estimate_extractor.py backend/app/ingestion/edq_extractor.py backend/app/ingestion/utility_plan_extractor.py backend/tests/test_eval_engine.py backend/tests/test_llm_logging.py
git commit -m "feat: route every LLM call through build_callbacks so failures log"
```

---

### Task 4: Outbound non-LLM failures — Supabase, Neo4j, JWKS, Storage

**Files:**
- Modify: `backend/app/database.py:33,44-46`
- Modify: `backend/app/neo4j_client.py:43,55-57`
- Modify: `backend/app/auth.py:68-69`
- Modify: `backend/app/api/auth.py:42-74`
- Modify: `backend/app/api/pdf.py:50-55`
- Create: `backend/tests/test_outbound_logging.py`

**Interfaces:**
- Consumes: `tests.logcapture.capture_logs` (Task 1).
- Produces: no new callable. These modules gain module-level `logger = logging.getLogger(__name__)` where they don't already have one.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_outbound_logging.py`:

```python
"""backend/tests/test_outbound_logging.py

Tests that non-LLM outbound calls -- Supabase, Neo4j, the Supabase admin
REST API over httpx, and Supabase Storage -- log when they fail, instead of
returning None/False silently.

All outbound calls are mocked; nothing here touches the network.

Runnable two ways:
    python tests/test_outbound_logging.py
    python -m pytest tests/test_outbound_logging.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.logcapture import capture_logs  # noqa: E402


def test_supabase_connection_failure_logs_an_error():
    from app.database import Database

    with patch("app.database.Database.get_client", side_effect=RuntimeError("dns failure")):
        with capture_logs("app.database") as records:
            assert Database.test_connection() is False

    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert "dns failure" in records[0].getMessage()


def test_neo4j_connection_failure_logs_an_error():
    from app.neo4j_client import Neo4jClient

    with patch("app.neo4j_client.Neo4jClient.get_graph", side_effect=RuntimeError("bolt refused")):
        with capture_logs("app.neo4j_client") as records:
            assert Neo4jClient.test_connection() is False

    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert "bolt refused" in records[0].getMessage()


def test_jwks_verification_failure_logs_a_warning():
    """Falling through from JWKS to the legacy HS256 secret is a normal,
    supported path (older Supabase projects) -- but today it is completely
    silent, so a genuinely unreachable JWKS endpoint looks identical to an
    old-format token."""
    from app import auth as auth_module

    class _FakeJWKSClient:
        @staticmethod
        def get_signing_key_from_jwt(token):
            raise RuntimeError("jwks endpoint unreachable")

    with patch.object(auth_module, "_get_jwks_client", return_value=_FakeJWKSClient()), \
         patch.object(auth_module.config, "SUPABASE_JWT_SECRET", ""), \
         patch("app.auth.jwt.decode", return_value={"sub": "user-1"}):
        with capture_logs("app.auth") as records:
            assert auth_module.user_id_from_token("Bearer sometoken") == "user-1"

    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "jwks endpoint unreachable" in records[0].getMessage()


def test_admin_user_lookup_transport_failure_logs_a_warning():
    from app.api import auth as api_auth

    with patch("app.api.auth.httpx.get", side_effect=RuntimeError("connection reset")):
        with capture_logs("app.api.auth") as records:
            assert api_auth._find_user_id_by_email("someone@example.com") is None

    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "connection reset" in records[0].getMessage()
    # The looked-up email must never reach the log.
    assert "someone@example.com" not in records[0].getMessage()


def test_admin_user_lookup_non_200_logs_a_warning():
    from app.api import auth as api_auth

    class _Resp:
        status_code = 503

    with patch("app.api.auth.httpx.get", return_value=_Resp()):
        with capture_logs("app.api.auth") as records:
            assert api_auth._find_user_id_by_email("someone@example.com") is None

    assert len(records) == 1
    assert "503" in records[0].getMessage()


def test_password_update_failure_logs_an_error():
    from app.api import auth as api_auth

    with patch("app.api.auth.httpx.put", side_effect=RuntimeError("timeout")):
        with capture_logs("app.api.auth") as records:
            assert api_auth._update_user_password("user-1", "hunter2hunter2") is False

    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    text = records[0].getMessage()
    assert "timeout" in text
    assert "hunter2hunter2" not in text  # never log the password


def test_password_update_non_200_logs_an_error():
    from app.api import auth as api_auth

    class _Resp:
        status_code = 422

    with patch("app.api.auth.httpx.put", return_value=_Resp()):
        with capture_logs("app.api.auth") as records:
            assert api_auth._update_user_password("user-1", "hunter2hunter2") is False

    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert "422" in records[0].getMessage()


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_outbound_logging.py -v`
Expected: all 7 FAIL — each asserts `len(records) == 1` but gets `0`, because every one of these paths currently `print`s or returns silently.

- [ ] **Step 3: Add logging to `database.py`**

In `backend/app/database.py`, find the imports at the top:

```python
from supabase import create_client, Client
from typing import Optional
from .config import config
```

Change to:

```python
import logging

from supabase import create_client, Client
from typing import Optional
from .config import config

logger = logging.getLogger(__name__)
```

Find:

```python
            print("OK Supabase client initialized")
```

Change to:

```python
            logger.info("Supabase client initialized")
```

Find:

```python
            client = cls.get_client()
            client.table("chunks").select("id").limit(1).execute()
            print("OK Database connection successful")
            return True
        except Exception as e:
            print(f"FAIL Database connection failed: {str(e)}")
            return False
```

Change to:

```python
            client = cls.get_client()
            client.table("chunks").select("id").limit(1).execute()
            logger.info("Supabase connection successful")
            return True
        except Exception as e:
            logger.error("Supabase connection failed: %s", e, exc_info=True)
            return False
```

- [ ] **Step 4: Add logging to `neo4j_client.py`**

In `backend/app/neo4j_client.py`, find the imports at the top:

```python
from typing import Optional

from langchain_neo4j import Neo4jGraph

from .config import config
```

Change to:

```python
import logging
from typing import Optional

from langchain_neo4j import Neo4jGraph

from .config import config

logger = logging.getLogger(__name__)
```

Find:

```python
            print("OK Neo4j client initialized")
```

Change to:

```python
            logger.info("Neo4j client initialized")
```

Find:

```python
            graph = cls.get_graph()
            graph.query("RETURN 1 AS ok")
            print("OK Neo4j connection successful")
            return True
        except Exception as e:
            print(f"FAIL Neo4j connection failed: {str(e)}")
            return False
```

Change to:

```python
            graph = cls.get_graph()
            graph.query("RETURN 1 AS ok")
            logger.info("Neo4j connection successful")
            return True
        except Exception as e:
            logger.error("Neo4j connection failed: %s", e, exc_info=True)
            return False
```

- [ ] **Step 5: Add logging to the JWKS fall-through in `app/auth.py`**

In `backend/app/auth.py`, find the imports at the top:

```python
from __future__ import annotations

import jwt
from fastapi import HTTPException

from app.config import config

_jwks_client: jwt.PyJWKClient | None = None
```

Change to:

```python
from __future__ import annotations

import logging

import jwt
from fastapi import HTTPException

from app.config import config

logger = logging.getLogger(__name__)

_jwks_client: jwt.PyJWKClient | None = None
```

Find:

```python
        except jwt.ExpiredSignatureError as exc:
            raise HTTPException(status_code=401, detail="Token expired") from exc
        except Exception:
            pass  # not a JWKS-signed token (or JWKS unreachable) — fall through
```

Change to:

```python
        except jwt.ExpiredSignatureError as exc:
            raise HTTPException(status_code=401, detail="Token expired") from exc
        except Exception as exc:
            # Expected for a legacy HS256-signed token, which the fallback
            # below handles — but identical in shape to a genuinely
            # unreachable JWKS endpoint, which is not expected at all. Log
            # it so the two stop looking the same in production.
            logger.warning("JWKS verification did not apply, falling back to HS256: %s", exc)
```

- [ ] **Step 6: Add logging to the Supabase admin API calls in `app/api/auth.py`**

In `backend/app/api/auth.py`, find the imports at the top:

```python
import hashlib
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import config

router = APIRouter(tags=["auth"])
```

Change to:

```python
import hashlib
import logging
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import config

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])
```

Find `_find_user_id_by_email`'s body:

```python
        if resp.status_code != 200:
            return None
        users = resp.json().get("users", [])
        match = next(
            (u for u in users if (u.get("email") or "").lower() == email.strip().lower()),
            None,
        )
        return str(match["id"]) if match else None
    except Exception:
        return None
```

Change to (the email address is deliberately absent from both messages — it is the caller-supplied identifier of a possibly-nonexistent account, and this endpoint exists partly to avoid confirming which addresses are registered):

```python
        if resp.status_code != 200:
            logger.warning(
                "Supabase admin user lookup returned %d — treating as not found",
                resp.status_code,
            )
            return None
        users = resp.json().get("users", [])
        match = next(
            (u for u in users if (u.get("email") or "").lower() == email.strip().lower()),
            None,
        )
        return str(match["id"]) if match else None
    except Exception as exc:
        logger.warning("Supabase admin user lookup failed: %s", exc)
        return None
```

Find `_update_user_password`'s body:

```python
        resp = httpx.put(
            f"{config.SUPABASE_URL}/auth/v1/admin/users/{user_id}",
            headers=_admin_headers(),
            json={"password": password},
            timeout=10.0,
        )
        return resp.status_code == 200
    except Exception:
        return False
```

Change to:

```python
        resp = httpx.put(
            f"{config.SUPABASE_URL}/auth/v1/admin/users/{user_id}",
            headers=_admin_headers(),
            json={"password": password},
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.error(
                "Supabase admin password update for user_id=%s returned %d",
                user_id, resp.status_code,
            )
            return False
        return True
    except Exception as exc:
        logger.error(
            "Supabase admin password update for user_id=%s failed: %s",
            user_id, exc, exc_info=True,
        )
        return False
```

- [ ] **Step 7: Add logging to the Storage not-found probe in `app/api/pdf.py`**

In `backend/app/api/pdf.py`, find the imports at the top:

```python
import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.config import config

router = APIRouter(tags=["pdf"])
```

Change to:

```python
import logging

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.config import config

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pdf"])
```

Find:

```python
        try:
            is_not_found = upstream.status_code in (400, 404) and b"not_found" in body
        except Exception:
            is_not_found = False
        if is_not_found:
            raise HTTPException(status_code=404, detail=f"PDF not found: {doc_name!r}")
        raise HTTPException(
            status_code=502,
            detail=f"PDF storage returned {upstream.status_code}",
        )
```

Change to:

```python
        try:
            is_not_found = upstream.status_code in (400, 404) and b"not_found" in body
        except Exception as exc:
            logger.warning(
                "Could not classify Storage response %d for doc=%r: %s",
                upstream.status_code, doc_name, exc,
            )
            is_not_found = False
        if is_not_found:
            logger.warning("PDF not found in Storage: doc=%r file=%r", doc_name, filename)
            raise HTTPException(status_code=404, detail=f"PDF not found: {doc_name!r}")
        logger.error(
            "Storage returned %d for doc=%r file=%r", upstream.status_code, doc_name, filename,
        )
        raise HTTPException(
            status_code=502,
            detail=f"PDF storage returned {upstream.status_code}",
        )
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_outbound_logging.py -v`
Expected: PASS (7 tests)

- [ ] **Step 9: Run the full backend suite to check for regressions**

Run: `.venv/Scripts/python.exe -m pytest backend/tests -q`
Expected: all tests pass. `test_review_pdf_endpoint.py` exercises `app.api.review`'s PDF route, not `app.api.pdf`, so it is unaffected.

- [ ] **Step 10: Commit**

```bash
git add backend/app/database.py backend/app/neo4j_client.py backend/app/auth.py backend/app/api/auth.py backend/app/api/pdf.py backend/tests/test_outbound_logging.py
git commit -m "feat: log Supabase, Neo4j, JWKS and Storage failures instead of swallowing them"
```

---

### Task 5: Ingestion failures that are currently silent

**Files:**
- Modify: `backend/app/ingestion/table_extractor.py:80-83` (imports), `:170-181`, `:185-205`, `:279-283`, `:312-316`
- Modify: `backend/app/ingestion/session_chunker.py:31-37` (imports), `:273-276`, `:289-294`, `:309-312`, `:319-321`
- Create: `backend/tests/test_ingestion_logging.py`

**Interfaces:**
- Consumes: `tests.logcapture.capture_logs` (Task 1).
- Produces: no new callable. Both modules gain `logger = logging.getLogger(__name__)`; `table_extractor.py` and `session_chunker.py` have none today.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_ingestion_logging.py`:

```python
"""backend/tests/test_ingestion_logging.py

Tests that PDF ingestion logs when it silently degrades. Every one of these
paths currently discards the exception and carries on with less data, which
is the right runtime behaviour and the wrong logging behaviour -- a garbled
table becomes a missing compliance citation with nothing in the log to
explain it.

Uses fake pdfplumber page objects; no real PDF is opened.

Runnable two ways:
    python tests/test_ingestion_logging.py
    python -m pytest tests/test_ingestion_logging.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.ingestion.table_extractor import TableExtractor  # noqa: E402
from tests.logcapture import capture_logs  # noqa: E402

_TABLE_LOGGER = "app.ingestion.table_extractor"
_CHUNKER_LOGGER = "app.ingestion.session_chunker"


class _ExplodingFindTablesPage:
    """A page whose find_tables() raises under both strict and relaxed
    settings -- the shape a malformed page graph produces."""

    width = 612
    height = 792

    def find_tables(self, table_settings=None):
        raise RuntimeError("bad page graph")


class _ExplodingTableObject:
    bbox = (0.0, 100.0, 500.0, 300.0)

    def extract(self):
        raise RuntimeError("cell extraction failed")


class _OneBadTablePage:
    """find_tables() succeeds but the single returned table blows up when
    extracted -- one bad table must not abort the page, but must be logged."""

    width = 612
    height = 792

    def find_tables(self, table_settings=None):
        return [_ExplodingTableObject()]

    def crop(self, bbox):
        raise RuntimeError("crop failed")


def test_find_tables_failure_logs_a_warning():
    with capture_logs(_TABLE_LOGGER) as records:
        result = TableExtractor().extract_tables(_ExplodingFindTablesPage(), page_pdf=441)

    assert result == []
    assert len(records) >= 1
    assert all(r.levelno == logging.WARNING for r in records)
    joined = " ".join(r.getMessage() for r in records)
    assert "441" in joined
    assert "bad page graph" in joined


def test_one_bad_table_logs_a_warning_and_does_not_abort_the_page():
    with capture_logs(_TABLE_LOGGER) as records:
        result = TableExtractor().extract_tables(_OneBadTablePage(), page_pdf=442)

    assert result == []  # the bad table is skipped, not raised
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) >= 1
    joined = " ".join(r.getMessage() for r in warnings)
    assert "442" in joined
    assert "cell extraction failed" in joined


def test_table_to_nl_fallback_logs_a_warning_when_pdfplumber_cannot_open():
    from app.ingestion.session_chunker import _extract_page_with_tables

    pages = [{"page_num": 1, "text": "original text", "char_count": 13}]
    with capture_logs(_CHUNKER_LOGGER) as records:
        result = _extract_page_with_tables("/definitely/not/a/real/file.pdf", pages)

    assert result == pages  # falls back to the original pages
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "/definitely/not/a/real/file.pdf" in records[0].getMessage()


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_ingestion_logging.py -v`
Expected: all 3 FAIL on `assert len(records) >= 1` / `== 1` getting `0` — these paths log nothing today.

- [ ] **Step 3: Add the logger to `table_extractor.py`**

In `backend/app/ingestion/table_extractor.py`, find the imports:

```python
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
```

Change to:

```python
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
```

- [ ] **Step 4: Log the per-table and per-page failures in `table_extractor.py`**

Find (in `extract_tables`):

```python
        results: List[Dict[str, Any]] = []
        for idx, tobj in enumerate(table_objects, start=1):
            try:
                result = self._process_table(
                    page, tobj, idx, page_pdf, page_printed
                )
                if result is not None:
                    results.append(result)
            except Exception:
                # One bad table should never abort the page
                pass

        return results
```

Change to:

```python
        results: List[Dict[str, Any]] = []
        for idx, tobj in enumerate(table_objects, start=1):
            try:
                result = self._process_table(
                    page, tobj, idx, page_pdf, page_printed
                )
                if result is not None:
                    results.append(result)
            except Exception as exc:
                # One bad table should never abort the page -- but a table
                # skipped here is a table the retrieval index never sees, so
                # it must not vanish silently.
                logger.warning(
                    "Skipped table %d on PDF page %d: %s", idx, page_pdf, exc,
                )

        return results
```

Find `_find_tables`:

```python
        try:
            tables = page.find_tables(table_settings=_SETTINGS_STRICT)
            if tables:
                return tables
        except Exception:
            pass

        try:
            tables = page.find_tables(table_settings=_SETTINGS_RELAXED)
            if tables:
                return tables
        except Exception:
            pass

        return []
```

Change to:

```python
        try:
            tables = page.find_tables(table_settings=_SETTINGS_STRICT)
            if tables:
                return tables
        except Exception as exc:
            logger.warning(
                "Strict table detection failed on PDF page %d, retrying relaxed: %s",
                page_pdf, exc,
            )

        try:
            tables = page.find_tables(table_settings=_SETTINGS_RELAXED)
            if tables:
                return tables
        except Exception as exc:
            logger.warning(
                "Relaxed table detection also failed on PDF page %d — no tables "
                "extracted from this page: %s",
                page_pdf, exc,
            )

        return []
```

`_find_tables` does not currently receive the page number. Change its signature from:

```python
    def _find_tables(self, page: Any) -> List[Any]:
```

to:

```python
    def _find_tables(self, page: Any, page_pdf: int) -> List[Any]:
```

and update its single caller, at the top of `extract_tables`:

```python
        table_objects = self._find_tables(page)
```

to:

```python
        table_objects = self._find_tables(page, page_pdf)
```

Find `_find_caption`'s crop guard:

```python
        try:
            caption_page = page.crop((0.0, scan_top, page.width, top))
            caption_text = caption_page.extract_text() or ""
        except Exception:
            caption_text = ""
```

Change to:

```python
        try:
            caption_page = page.crop((0.0, scan_top, page.width, top))
            caption_text = caption_page.extract_text() or ""
        except Exception as exc:
            logger.warning(
                "Caption scan failed for table %d on PDF page %d — the table will "
                "fall back to a positional id: %s",
                idx, page_pdf, exc,
            )
            caption_text = ""
```

Find `_find_footnotes`'s crop guard:

```python
        try:
            fn_page = page.crop((0.0, bottom, page.width, scan_bottom))
            fn_text = fn_page.extract_text() or ""
        except Exception:
            return []
```

Change to:

```python
        try:
            fn_page = page.crop((0.0, bottom, page.width, scan_bottom))
            fn_text = fn_page.extract_text() or ""
        except Exception as exc:
            logger.warning("Footnote scan failed below a table: %s", exc)
            return []
```

- [ ] **Step 5: Add the logger to `session_chunker.py`**

In `backend/app/ingestion/session_chunker.py`, find the imports:

```python
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

import tiktoken
```

Change to:

```python
from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

import tiktoken

logger = logging.getLogger(__name__)
```

- [ ] **Step 6: Log the table→NL fallbacks in `session_chunker.py`**

In `_extract_page_with_tables`, find:

```python
    try:
        import pdfplumber as _plumber
    except ImportError:
        return pages
```

Change to:

```python
    try:
        import pdfplumber as _plumber
    except ImportError:
        logger.warning(
            "pdfplumber unavailable — Special Provision tables will be chunked as "
            "raw linearized text instead of natural-language sentences",
        )
        return pages
```

Find the per-page table-find guard:

```python
                try:
                    finders     = pdf_page.find_tables()
                    tables_data = [f.extract() for f in finders]
                    table_bboxes = [f.bbox for f in finders]
                except Exception:
                    enriched.append(page_dict)
                    continue
```

Change to:

```python
                try:
                    finders     = pdf_page.find_tables()
                    tables_data = [f.extract() for f in finders]
                    table_bboxes = [f.bbox for f in finders]
                except Exception as exc:
                    logger.warning(
                        "Table extraction failed on PDF page %d — keeping the raw "
                        "page text: %s",
                        p_num, exc,
                    )
                    enriched.append(page_dict)
                    continue
```

Find the non-table-text guard:

```python
                    try:
                        non_table_text = pdf_page.filter(_outside).extract_text() or ""
                    except Exception:
                        non_table_text = page_dict["text"]
```

Change to:

```python
                    try:
                        non_table_text = pdf_page.filter(_outside).extract_text() or ""
                    except Exception as exc:
                        logger.warning(
                            "Could not separate non-table text on PDF page %d — the "
                            "table text may appear twice in this chunk: %s",
                            p_num, exc,
                        )
                        non_table_text = page_dict["text"]
```

Find the whole-document guard at the end of the function:

```python
    except Exception:
        return pages

    return enriched
```

Change to:

```python
    except Exception as exc:
        logger.warning(
            "Could not reopen %s for table extraction — chunking the raw text "
            "instead: %s",
            pdf_path, exc,
        )
        return pages

    return enriched
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_ingestion_logging.py -v`
Expected: PASS (3 tests)

- [ ] **Step 8: Run the full backend suite to check for regressions**

Run: `.venv/Scripts/python.exe -m pytest backend/tests -q`
Expected: all tests pass. The only signature change is `_find_tables`, which is private and has exactly one caller (updated in Step 4).

- [ ] **Step 9: Commit**

```bash
git add backend/app/ingestion/table_extractor.py backend/app/ingestion/session_chunker.py backend/tests/test_ingestion_logging.py
git commit -m "feat: log PDF table-extraction failures instead of degrading silently"
```

---

### Task 6: Degraded compliance-review paths

**Files:**
- Modify: `backend/app/api/review.py:170-187` (`_validate_owned_path`), `:549-576` (`_seed_edq_items_if_needed`)
- Modify: `backend/app/compliance/eval_engine.py:483-499` (missing-sources short-circuit)
- Create: `backend/tests/test_review_degraded_logging.py`

**Interfaces:**
- Consumes: `tests.logcapture.capture_logs` (Task 1).
- Produces: no new callable. Both modules already define `logger = logging.getLogger(__name__)`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_review_degraded_logging.py`:

```python
"""backend/tests/test_review_degraded_logging.py

Tests that the review pipeline logs when it quietly produces a worse
result: a rejected Storage path, an EDQ seeding step that gives up, or a
check that reports Missing because a document it needed was never
uploaded. All three are deliberate, correct runtime behaviour -- and all
three are invisible in the log today, which is what makes "why is this
check Missing?" unanswerable from the console.

Runnable two ways:
    python tests/test_review_degraded_logging.py
    python -m pytest tests/test_review_degraded_logging.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import HTTPException  # noqa: E402

from app.api.review import _seed_edq_items_if_needed, _validate_owned_path  # noqa: E402
from app.compliance.catalog import CheckDef  # noqa: E402
from app.compliance.eval_engine import _DeterministicContext, _evaluate_one_check  # noqa: E402
from tests.logcapture import capture_logs  # noqa: E402

_REVIEW_LOGGER = "app.api.review"
_EVAL_LOGGER = "app.compliance.eval_engine"


def test_rejected_storage_path_logs_a_warning():
    """A 403 from _validate_owned_path is either a client bug or someone
    probing for another user's documents. Either way it should be visible."""
    with capture_logs(_REVIEW_LOGGER) as records:
        try:
            _validate_owned_path("other-user/p1/schedule.xer", "user-1")
            assert False, "Should have raised HTTPException"
        except HTTPException as exc:
            assert exc.status_code == 403

    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    text = records[0].getMessage()
    assert "user-1" in text
    assert "other-user/p1/schedule.xer" in text


def test_malformed_storage_path_logs_a_warning():
    with capture_logs(_REVIEW_LOGGER) as records:
        try:
            _validate_owned_path("user-1/%2Fx/y", "user-1")
            assert False, "Should have raised HTTPException"
        except HTTPException as exc:
            assert exc.status_code == 403

    assert len(records) == 1
    assert records[0].levelno == logging.WARNING


def test_edq_seeding_skipped_without_an_estimate_logs_a_warning():
    graph = MagicMock()
    graph.query.return_value = [{"c": 0}]

    with capture_logs(_REVIEW_LOGGER) as records:
        _seed_edq_items_if_needed(
            graph, llm=MagicMock(), embeddings=MagicMock(), estimate_bytes=None,
            project_id="p1", user_id="user-1",
        )

    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    text = records[0].getMessage()
    assert "p1" in text
    assert "estimate" in text.lower()


def test_edq_seeding_with_no_activities_logs_a_warning():
    graph = MagicMock()
    # First query: the EdqItem existence count. Second: the activity roster.
    graph.query.side_effect = [[{"c": 0}], []]

    class _Extraction:
        items = [object()]

    import app.api.review as review_module

    original = review_module.extract_edq_items
    review_module.extract_edq_items = lambda *a, **k: _Extraction()
    try:
        with capture_logs(_REVIEW_LOGGER) as records:
            _seed_edq_items_if_needed(
                graph, llm=MagicMock(), embeddings=MagicMock(), estimate_bytes=b"pdf",
                project_id="p1", user_id="user-1",
            )
    finally:
        review_module.extract_edq_items = original

    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "activities" in records[0].getMessage().lower()


def test_already_seeded_edq_logs_nothing():
    """The normal rerun path is not a degradation and must stay quiet."""
    graph = MagicMock()
    graph.query.return_value = [{"c": 12}]

    with capture_logs(_REVIEW_LOGGER, logging.DEBUG) as records:
        _seed_edq_items_if_needed(
            graph, llm=MagicMock(), embeddings=MagicMock(), estimate_bytes=b"pdf",
            project_id="p1", user_id="user-1",
        )

    assert records == []


def test_check_missing_for_unavailable_source_logs_a_warning():
    check = CheckDef(
        check_key="summer_shutdown", category="", name="Summer Shutdown",
        instruction="check it", source_files=["sp"],
    )

    with capture_logs(_EVAL_LOGGER) as records:
        result, usage = _evaluate_one_check(
            check, MagicMock(), MagicMock(),
            schedule_facts="", narrative_text="",
            sp_search_fn=None, spec_search_fn=None, csm_search_fn=None,
            keymap_facts=None, estimate_facts=None, utility_plan_search_fn=None,
            deterministic=_DeterministicContext(),
            project_id="p1", user_id="user-1", callbacks=[],
        )

    assert result.status == "Missing"
    assert usage["llm_call_count"] == 0
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    text = records[0].getMessage()
    assert "summer_shutdown" in text
    assert "Special Provision" in text


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_review_degraded_logging.py -v`
Expected: 5 FAIL on `len(records)` being `0`; `test_already_seeded_edq_logs_nothing` PASSES already (nothing logs today, which is the correct end state for that one).

- [ ] **Step 3: Log rejected Storage paths in `review.py`**

In `backend/app/api/review.py`, find `_validate_owned_path`'s two rejection points:

```python
    try:
        parts = _relative_path_parts(path)
        resolved = _PATH_RESOLUTION_BASE.joinpath(*parts).parts[1:]
    except Exception:
        # yarl can raise on more than just a doubly-encoded "%2Fx" segment
        # (e.g. a "//host"-prefixed path making it parse a bogus host, or
        # a segment that fails IDNA encoding) -- any parse failure here
        # means the input is malformed/adversarial, not a legitimate
        # value, so it's rejected the same way as a resolved-but-wrong
        # path rather than propagating as an unhandled 500.
        raise HTTPException(status_code=403, detail="Storage path does not belong to you.")
    if (
        len(resolved) < 4
        or resolved[0] != "object"
        or resolved[1] != _STORAGE_BUCKET
        or resolved[2] != user_id
    ):
        raise HTTPException(status_code=403, detail="Storage path does not belong to you.")
```

Change to:

```python
    try:
        parts = _relative_path_parts(path)
        resolved = _PATH_RESOLUTION_BASE.joinpath(*parts).parts[1:]
    except Exception as exc:
        # yarl can raise on more than just a doubly-encoded "%2Fx" segment
        # (e.g. a "//host"-prefixed path making it parse a bogus host, or
        # a segment that fails IDNA encoding) -- any parse failure here
        # means the input is malformed/adversarial, not a legitimate
        # value, so it's rejected the same way as a resolved-but-wrong
        # path rather than propagating as an unhandled 500.
        logger.warning(
            "Rejected unparseable Storage path %r for user_id=%s: %s", path, user_id, exc,
        )
        raise HTTPException(status_code=403, detail="Storage path does not belong to you.")
    if (
        len(resolved) < 4
        or resolved[0] != "object"
        or resolved[1] != _STORAGE_BUCKET
        or resolved[2] != user_id
    ):
        logger.warning(
            "Rejected Storage path %r for user_id=%s (resolves to %s)",
            path, user_id, "/".join(resolved),
        )
        raise HTTPException(status_code=403, detail="Storage path does not belong to you.")
```

- [ ] **Step 4: Log the EDQ seeding give-up points in `review.py`**

Find `_seed_edq_items_if_needed`'s body, from the existence check to the seed call:

```python
    existing = graph.query(
        "MATCH (e:EdqItem {projectId: $pid}) RETURN count(e) AS c LIMIT 1",
        params={"pid": project_id},
    )
    if existing and existing[0]["c"]:
        return
    if not estimate_bytes:
        return
    extraction = extract_edq_items(estimate_bytes, llm, project_id=project_id, user_id=user_id)
    if extraction is None or not extraction.items:
        return
    activities = graph.query(
        "MATCH (a:Activity {projectId: $pid}) "
        "RETURN a.taskId AS taskId, a.name AS name, a.wbsPath AS wbsPath",
        params={"pid": project_id},
    ) or []
    if not activities:
        return
```

Change to (the first return is the normal already-seeded rerun path and stays silent; every other one means the `edq_items` check will report Missing or Fail for a reason nobody can currently see):

```python
    existing = graph.query(
        "MATCH (e:EdqItem {projectId: $pid}) RETURN count(e) AS c LIMIT 1",
        params={"pid": project_id},
    )
    if existing and existing[0]["c"]:
        return
    if not estimate_bytes:
        logger.warning(
            "EDQ seeding skipped for project_id=%s: no estimate document uploaded — "
            "the edq_items check will report Missing",
            project_id,
        )
        return
    extraction = extract_edq_items(estimate_bytes, llm, project_id=project_id, user_id=user_id)
    if extraction is None or not extraction.items:
        logger.warning(
            "EDQ seeding skipped for project_id=%s: no line items could be read from "
            "the estimate document — the edq_items check will report Missing",
            project_id,
        )
        return
    activities = graph.query(
        "MATCH (a:Activity {projectId: $pid}) "
        "RETURN a.taskId AS taskId, a.name AS name, a.wbsPath AS wbsPath",
        params={"pid": project_id},
    ) or []
    if not activities:
        logger.warning(
            "EDQ seeding skipped for project_id=%s: the schedule graph holds no "
            "activities to match against",
            project_id,
        )
        return
```

Then find the match-failure return a few lines below:

```python
    if matches is None:
        return  # matching failed -- don't seed a false "all uncovered" state; retry next run
```

Change to:

```python
    if matches is None:
        logger.warning(
            "EDQ matching failed for project_id=%s — not seeding, so the next rerun "
            "retries instead of recording a false 'all uncovered' result",
            project_id,
        )
        return
```

- [ ] **Step 5: Log the missing-sources short-circuit in `eval_engine.py`**

In `backend/app/compliance/eval_engine.py`, find the end of the `missing_sources` block:

```python
    if missing_sources:
        return ReviewCheckResult(
            id=check.check_key, category=check.category, name=check.name,
            status="Missing", evidence=f"Not available for this review: {', '.join(missing_sources)}.",
            source="no data provided",
        ), usage_totals
```

Change to:

```python
    if missing_sources:
        logger.warning(
            "Check %s reported Missing without an LLM call: %s",
            check.check_key, ", ".join(missing_sources),
        )
        return ReviewCheckResult(
            id=check.check_key, category=check.category, name=check.name,
            status="Missing", evidence=f"Not available for this review: {', '.join(missing_sources)}.",
            source="no data provided",
        ), usage_totals
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_review_degraded_logging.py -v`
Expected: PASS (6 tests)

- [ ] **Step 7: Run the full backend suite to check for regressions**

Run: `.venv/Scripts/python.exe -m pytest backend/tests -q`
Expected: all tests pass. `test_review_endpoint.py`, `test_review_rerun_endpoint.py` and `test_review_pdf_endpoint.py` all exercise `_validate_owned_path`'s 403 paths and assert only on the status code, so the added log lines do not affect them.

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/review.py backend/app/compliance/eval_engine.py backend/tests/test_review_degraded_logging.py
git commit -m "feat: log rejected storage paths, skipped EDQ seeding and evidence-less checks"
```

---

## Manual verification (not automated — needs live credentials)

The tests fake every outbound call, so they prove the log lines fire on the
right branches but not that the wiring survives a real request. After all
six tasks land, with `backend/.env` populated:

1. Start the backend: `.venv/Scripts/python.exe -m uvicorn app.main:app --reload --app-dir backend`
2. `curl -i http://localhost:8000/api/pdf/NoSuchDoc` — console shows an
   `app.api.pdf` WARNING for the Storage miss and an `app.request` WARNING
   `GET /api/pdf/NoSuchDoc -> 404 after Nms`.
3. `curl -i -X POST http://localhost:8000/api/query -H 'Content-Type: application/json' -d '{"query":""}'`
   — console shows one `app.request` WARNING for the 400 and nothing else.
4. Temporarily set `OPENAI_API_KEY` to a junk value and submit a review
   from the UI — console shows `app.llm` ERROR lines reading
   `LLM call failed [evaluate-check:<key>]: ...`, then, because
   `.with_fallbacks()` switches to Claude, the review still completes. This
   is the failure that is entirely invisible today; it is the single
   clearest confirmation that Tasks 2 and 3 work.
5. Submit a review with no DBE Goal Memo — console shows the
   `EDQ seeding skipped for project_id=... no estimate document uploaded`
   WARNING and one `Check edq_items reported Missing without an LLM call`
   WARNING.
6. Open the review's SSE stream in the browser and confirm no
   `token=<jwt>` string appears anywhere in the console output.
