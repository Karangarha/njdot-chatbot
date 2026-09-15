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

# Third-party loggers that emit at INFO on a per-request or per-page basis.
# Left at INFO they dominate the console: httpx logs one 'HTTP Request: ...'
# line for EVERY outbound call, so a single compliance review (60+ LLM calls
# plus every Supabase and Storage round trip) buries its own findings, and
# pdfminer narrates each page of a 671-page spec ingest.
#
# Set to WARNING, not disabled -- a real problem in any of these still
# surfaces, and this app's own app.* loggers now cover the failures that
# matter with far better attribution than a bare status line.
_NOISY_DEPENDENCIES = (
    "httpx", "httpcore", "hpack",          # one line per outbound request
    "openai", "anthropic",                  # SDK request plumbing
    "langchain", "langchain_core", "langchain_openai", "langchain_classic",
    "langgraph", "langgraph_sdk", "langsmith",
    "neo4j", "neo4j_graphrag",
    "pdfminer", "pdfplumber", "PIL",        # per-page during ingestion
    "urllib3", "requests", "charset_normalizer",
    "filelock", "asyncio", "dotenv", "tqdm", "opentelemetry",
)


class _AccessLogHygiene(logging.Filter):
    """Cleans up uvicorn's own access log. Two jobs, both load-bearing.

    **Strips query strings.** uvicorn formats its access line from
    ``get_path_with_query_string(scope)``, which appends the raw query
    string -- so ``GET /api/review/{id}/status?token=<supabase jwt>`` writes
    a live access token into the console on every connection. The middleware
    in this module deliberately logs ``request.url.path`` for exactly this
    reason; uvicorn does not, so it is redacted here instead.

    **Drops the SSE progress streams.** ``/api/review/{id}/status`` and
    ``/api/session/status/{id}`` are opened once per review or upload and
    held for its whole duration, reconnecting on any blip. Their access
    lines say nothing the review's own log lines do not say better.

    Reads ``record.args``, which uvicorn packs as
    ``(client_addr, method, full_path, http_version, status_code)`` -- see
    ``uvicorn.logging.AccessFormatter.formatMessage``. Any record not in
    that shape is passed through untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        full_path = args[2]
        if not isinstance(full_path, str):
            return True
        path = full_path.split("?", 1)[0]
        if "/status" in path:
            return False
        if "?" in full_path:
            record.args = (args[0], args[1], path + "?<redacted>", args[3], args[4])
        return True


def configure_console_logging() -> None:
    """Quiet the dependencies and clean up uvicorn's access log.

    Called once from ``app.main`` right after ``logging.basicConfig``. Both
    halves are about signal-to-noise on a deployed console (Azure App
    Service's log stream is where this app is actually read), not about
    changing what this application itself reports.
    """
    for name in _NOISY_DEPENDENCIES:
        logging.getLogger(name).setLevel(logging.WARNING)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _AccessLogHygiene) for f in access.filters):
        access.addFilter(_AccessLogHygiene())


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
