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
