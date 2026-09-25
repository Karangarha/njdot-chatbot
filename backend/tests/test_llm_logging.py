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


def test_llm_error_logs_a_warning_because_a_fallback_may_still_recover():
    """One provider attempt failed; .with_fallbacks() may answer on Claude and
    return a correct result. The handler cannot see that outcome, so it logs
    the degraded-path level and leaves ERROR to the call site, which can."""
    handler = LLMLoggingCallbackHandler(operation="evaluate-check:no_lag")
    with capture_logs(_LOGGER_NAME) as records:
        handler.on_llm_error(RuntimeError("429 rate limit"), run_id=uuid4())

    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    text = records[0].getMessage()
    assert "evaluate-check:no_lag" in text
    assert "429 rate limit" in text
    assert records[0].exc_info is None  # WARNING carries no traceback


def test_llm_error_without_operation_still_logs():
    """operation is optional -- a call site that hasn't named its operation
    must still produce a usable line, not crash on a None format arg."""
    handler = LLMLoggingCallbackHandler()
    with capture_logs(_LOGGER_NAME) as records:
        handler.on_llm_error(ValueError("bad request"), run_id=uuid4())

    assert len(records) == 1
    assert "bad request" in records[0].getMessage()


def test_tool_and_retriever_errors_log_an_error_with_traceback():
    """Nothing retries these: the agent gets a failed tool result and answers
    without it, so this line is the only record the call ever happened."""
    handler = LLMLoggingCallbackHandler(operation="chat-agent-response")
    with capture_logs(_LOGGER_NAME) as records:
        handler.on_tool_error(RuntimeError("cypher exploded"), run_id=uuid4())
        handler.on_retriever_error(RuntimeError("pgvector down"), run_id=uuid4())

    assert len(records) == 2
    assert all(r.levelno == logging.ERROR for r in records)
    assert all(r.exc_info is not None for r in records)
    joined = " ".join(r.getMessage() for r in records)
    assert "cypher exploded" in joined
    assert "pgvector down" in joined


def test_chain_errors_are_deliberately_not_logged():
    """LangChain fires on_chain_error at EVERY level of a nested runnable, and
    with_structured_output(include_raw=True).with_fallbacks([...]) is several
    levels deep -- implementing it printed one failure, traceback and all, two
    or three times over. The inner on_llm_error/on_tool_error already reported
    the cause. This pins the decision against a future "completeness" edit."""
    assert "on_chain_error" not in vars(LLMLoggingCallbackHandler), (
        "on_chain_error is back -- see the handler docstring for why it is not implemented"
    )

    handler = LLMLoggingCallbackHandler(operation="evaluate-check:no_lag")
    with capture_logs(_LOGGER_NAME, logging.DEBUG) as records:
        handler.on_chain_error(RuntimeError("chain broke"), run_id=uuid4())
    assert records == []


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


def test_the_review_labels_each_check_not_the_whole_review():
    """The per-check label is the reason this logging exists: with one handler
    shared across all 57 checks, every LLM failure in a review printed the same
    operation and named none of them. build_callbacks() therefore has to be
    called inside the submit loop, where check_key is in scope."""
    source = (_ROOT / "app/compliance/eval_engine.py").read_text(encoding="utf-8")
    assert 'operation=f"evaluate-check:{check.check_key}"' in source, (
        "eval_engine no longer labels its callbacks per check"
    )


def test_every_llm_call_site_uses_build_callbacks():
    for relative_path in _CALLBACK_SITE_MODULES:
        source = (_ROOT / relative_path).read_text(encoding="utf-8")
        assert "build_callbacks(" in source, f"{relative_path} never calls build_callbacks()"
        assert "get_langfuse_handler(" not in source, (
            f"{relative_path} still builds a Langfuse-only callback list — "
            "its LLM failures will not be logged"
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
