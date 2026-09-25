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
* **A hook that would never fire, so it is not here.** ``on_retry`` is part of the
  callback API, but langchain-core 1.4.9 emits it only from the legacy ``BaseLLM``
  retry decorator — never from ChatOpenAI/ChatAnthropic, whose retries happen
  inside the provider SDK below the callback layer. Those retries surface instead
  as the SDK's own ``Retrying request to ...`` line, which ``app.request_logging``
  deliberately leaves unsilenced.

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
    """Logs LangChain error events to the console.

    ``operation`` is the same human-readable label the call site already
    passes as LangChain's ``run_name`` (e.g. ``"evaluate-check:no_lag"``,
    ``"extract-key-map"``), so a log line can be matched to the code that
    produced it. Optional — a site without one still logs usefully.

    Every method swallows nothing and raises nothing: a callback that
    raised would propagate into the caller's ``.invoke()`` and turn an
    observability concern into an outage.

    ``on_chain_error`` is deliberately NOT implemented. LangChain fires it at
    every level of a nested runnable, so one failure inside
    ``with_structured_output(..., include_raw=True).with_fallbacks([...])``
    logged the identical line — traceback and all — once per level. A chain
    error is always a re-raise of an inner error the methods below have
    already reported, so nothing is lost by staying quiet.
    """

    def __init__(self, operation: Optional[str] = None) -> None:
        super().__init__()
        self.operation = operation or "llm call"

    # ── A provider call failed. Whether the OPERATION failed is not knowable
    # from here: ``.with_fallbacks()`` may still recover it on Claude, and
    # the call site logs its own ERROR when it does not (eval_engine and the
    # extractors catch and ``logger.exception``; app.api.session lets it
    # reach the request middleware). So this is the degraded-path level --
    # WARNING, no traceback, as everywhere else in this app. ───────────────

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        logger.warning("LLM call failed [%s]: %s", self.operation, error)

    # ── Tools and retrievers have no fallback layer above them: the agent
    # gets a failed tool result and answers without it, so these lines are
    # the only record the call happened at all -> ERROR + traceback. ───────

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        logger.error("Tool call failed [%s]: %s", self.operation, error, exc_info=error)

    def on_retriever_error(self, error: BaseException, **kwargs: Any) -> None:
        logger.error("Retriever call failed [%s]: %s", self.operation, error, exc_info=error)


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

    One deliberate behavior change from the per-site code this replaced: the
    extractors used to build ``get_langfuse_handler(...) if project_id else
    None``, so a call without a ``project_id`` got no Langfuse handler at
    all. Here a handler is built either way, and one with ``trace_id=None``
    starts its own top-level trace. Every real caller passes a project_id
    (``app.api.review`` and ``app.api.session`` both thread one through), so
    this only shows up for a direct call that omits it — and the no-trace
    handler is exactly what ``app.api.session``'s chat path has always used.
    """
    callbacks: List[Any] = [LLMLoggingCallbackHandler(operation=operation)]
    try:
        langfuse_handler = get_langfuse_handler(
            trace_id=trace_id, parent_span_id=parent_span_id,
        )
    except Exception as exc:
        # WARNING, so no traceback: tracing being down is a degraded path we
        # continue through, not a failure of the call itself.
        logger.warning("Langfuse callback unavailable — continuing with logging only: %s", exc)
        langfuse_handler = None
    if langfuse_handler is not None:
        callbacks.append(langfuse_handler)
    return callbacks
