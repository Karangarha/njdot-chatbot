"""backend/tests/test_eval_engine.py

Tests for app.compliance.eval_engine.evaluate_checks's on_progress callback
(granular review-progress reporting). Mocks the check-evaluation and
graph-reading internals entirely — no real Neo4j or LLM calls.

Runnable two ways:
    python tests/test_eval_engine.py
    python -m pytest tests/test_eval_engine.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.compliance.catalog import CheckDef  # noqa: E402
from app.compliance.eval_engine import evaluate_checks  # noqa: E402
from app.models import ReviewCheckResult  # noqa: E402


def _fake_checks(n: int) -> list[CheckDef]:
    return [
        CheckDef(check_key=f"c{i}", category="Cat", name=f"Check {i}", instruction="do it")
        for i in range(n)
    ]


def test_evaluate_checks_reports_progress_via_on_progress_callback():
    checks = _fake_checks(3)
    fake_result = ReviewCheckResult(
        id="c0", category="Cat", name="Check 0", status="Pass", evidence="e", source="schedule",
    )
    fake_usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "llm_call_count": 0}

    progress_calls: list[tuple[int, int]] = []

    def on_progress(done: int, total: int) -> None:
        progress_calls.append((done, total))

    with patch("app.compliance.eval_engine.build_compliance_facts", return_value=""), \
         patch("app.compliance.eval_engine.build_milestones", return_value=""), \
         patch("app.compliance.eval_engine.build_activity_roster", return_value=""), \
         patch("app.compliance.eval_engine.build_narrative_text", return_value=""), \
         patch("app.compliance.eval_engine._evaluate_one_check", return_value=(fake_result, fake_usage)):
        evaluate_checks(checks, graph=MagicMock(), llm=MagicMock(), on_progress=on_progress)

    assert progress_calls == [(1, 3), (2, 3), (3, 3)]


def test_evaluate_checks_works_without_on_progress():
    """on_progress is optional -- existing callers that don't pass it must
    be unaffected (default None, never invoked, no crash)."""
    checks = _fake_checks(2)
    fake_result = ReviewCheckResult(
        id="c0", category="Cat", name="Check 0", status="Pass", evidence="e", source="schedule",
    )
    fake_usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "llm_call_count": 0}

    with patch("app.compliance.eval_engine.build_compliance_facts", return_value=""), \
         patch("app.compliance.eval_engine.build_milestones", return_value=""), \
         patch("app.compliance.eval_engine.build_activity_roster", return_value=""), \
         patch("app.compliance.eval_engine.build_narrative_text", return_value=""), \
         patch("app.compliance.eval_engine._evaluate_one_check", return_value=(fake_result, fake_usage)):
        results = evaluate_checks(checks, graph=MagicMock(), llm=MagicMock())

    assert len(results) == 2


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
