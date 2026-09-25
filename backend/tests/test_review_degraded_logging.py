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
            schedule_facts="", narrative_result=("", {}),
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
