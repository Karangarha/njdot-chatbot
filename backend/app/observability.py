"""LangChain call tracing via a self-hosted Langfuse instance.

Purely for observability/debugging — visualizing how the review pipeline
(``app.compliance.eval_engine``) and the Document Q&A agent
(``app.api.session``) actually invoke LangChain. Not required for either
pipeline to function; every function here fails soft (returns ``None`` on
any error) so tracing being down/unconfigured never breaks a review or chat
query. Follows the langfuse/skills instrumentation best practices:
https://langfuse.com/docs/observability/best-practices

Trace grouping across threads: ``app.compliance.eval_engine.evaluate_checks``
dispatches one call per check across a ``ThreadPoolExecutor`` — LangChain/
Langfuse's contextvars-based tracing context does not automatically
propagate across ``ThreadPoolExecutor.submit()`` boundaries, so instead of
each concurrent check becoming its own top-level trace, ``new_trace_id``
generates a deterministic trace id from a seed (e.g. the review's
project_id) up front, and every worker's ``CallbackHandler`` is built with
that same ``trace_id`` (see ``get_langfuse_handler``) — Langfuse merges them
into one trace server-side without needing any actual object/context to
cross the thread boundary.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_langfuse_client():
    """Return the process-wide Langfuse client singleton, initialized lazily
    from ``LANGFUSE_PUBLIC_KEY``/``LANGFUSE_SECRET_KEY``/``LANGFUSE_BASE_URL``
    env vars (must be imported after those are loaded — see
    ``app.config``'s ``load_dotenv()`` calls, which always run first since
    every module here imports ``app.config`` before anything else). Returns
    ``None`` if the SDK can't be imported/constructed at all.
    """
    try:
        from langfuse import get_client
        return get_client()
    except Exception:
        logger.warning("Langfuse client unavailable", exc_info=True)
        return None


def new_trace_id(seed: str) -> Optional[str]:
    """Deterministic trace id derived from ``seed`` (e.g. a review's
    project_id) — the same seed always produces the same trace id, so
    independent calls (including ones on different threads) can attach to
    the same trace without sharing any Python object. Returns ``None`` if
    Langfuse isn't available.
    """
    client = get_langfuse_client()
    if client is None:
        return None
    try:
        return client.create_trace_id(seed=seed)
    except Exception:
        logger.warning("Failed to create Langfuse trace id", exc_info=True)
        return None


def get_langfuse_handler(trace_id: Optional[str] = None, parent_span_id: Optional[str] = None):
    """Build a ``langfuse.langchain.CallbackHandler``, optionally attached to
    a specific trace/parent span (see ``new_trace_id``) so multiple
    independent ``.invoke()`` calls nest under one shared trace instead of
    each becoming its own top-level trace. Fails soft: returns ``None`` on
    any construction error, so callers simply omit the callback.
    """
    try:
        from langfuse.langchain import CallbackHandler
        trace_context = None
        if trace_id:
            trace_context = {"trace_id": trace_id}
            if parent_span_id:
                trace_context["parent_span_id"] = parent_span_id
        return CallbackHandler(trace_context=trace_context)
    except Exception:
        logger.warning("Langfuse tracing unavailable — continuing without it", exc_info=True)
        return None
