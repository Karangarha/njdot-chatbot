"""backend/tests/test_eval_engine.py

Tests for app.compliance.eval_engine.evaluate_checks's on_progress callback
(granular review-progress reporting) and for the grounding-judge second-pass
check. Mocks the check-evaluation and graph-reading internals entirely — no
real Neo4j or LLM calls.

Runnable two ways:
    python tests/test_eval_engine.py
    python -m pytest tests/test_eval_engine.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.compliance.catalog import CheckDef  # noqa: E402
from app.compliance.eval_engine import (  # noqa: E402
    _DeterministicContext,
    _accumulate_usage,
    _evaluate_one_check,
    _judge_grounding,
    _retry_with_correction,
    evaluate_checks,
)
from app.config import config  # noqa: E402
from app.models import EvaluationSchema, GroundingJudgment, ReviewCheckResult  # noqa: E402


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
    fake_usage = {
        "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "llm_call_count": 0,
        "judged": 0, "ungrounded": 0, "downgraded": 0,
    }

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
    fake_usage = {
        "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "llm_call_count": 0,
        "judged": 0, "ungrounded": 0, "downgraded": 0,
    }

    with patch("app.compliance.eval_engine.build_compliance_facts", return_value=""), \
         patch("app.compliance.eval_engine.build_milestones", return_value=""), \
         patch("app.compliance.eval_engine.build_activity_roster", return_value=""), \
         patch("app.compliance.eval_engine.build_narrative_text", return_value=""), \
         patch("app.compliance.eval_engine._evaluate_one_check", return_value=(fake_result, fake_usage)):
        results = evaluate_checks(checks, graph=MagicMock(), llm=MagicMock())

    assert len(results) == 2


class _FakeStructuredLLM:
    """Stub for llm.with_structured_output(Schema, include_raw=True).invoke().
    Queues one (parsed, usage_metadata) pair per call, consumed in order, so
    a test can script an initial answer followed by a retry answer."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def invoke(self, messages, config=None):
        self.calls.append(messages)
        parsed, usage_metadata = self._responses.pop(0)
        return {
            "raw": SimpleNamespace(usage_metadata=usage_metadata),
            "parsed": parsed,
            "parsing_error": None,
        }


class _FakeErroringLLM:
    """Stub whose .invoke() always raises, to test fail-open/fail-safe paths."""

    def invoke(self, messages, config=None):
        raise RuntimeError("simulated LLM failure")


def _make_check(**overrides):
    defaults = dict(
        check_key="row_availability",
        category="Environmental, Landscape & Utilities",
        name="ROW Availability Date Precedes Parcel Work",
        instruction="No parcel work may start before that parcel's availability date.",
    )
    defaults.update(overrides)
    return CheckDef(**defaults)


def test_judge_grounding_parses_response():
    check = _make_check()
    result = EvaluationSchema(
        status="Fail",
        evidence="B1010 starts 2024-09-01, ROW available 2024-08-01.",
        source="B1010",
    )
    fake_llm = _FakeStructuredLLM([
        (
            GroundingJudgment(grounded=False, reason="Dates show compliance, not violation."),
            {"input_tokens": 50, "output_tokens": 10, "input_token_details": {"cache_read": 0}},
        ),
    ])

    judgment, usage = _judge_grounding(
        check, "ROW parcel available 2024-08-01. B1010 starts 2024-09-01.", result, fake_llm, {},
    )

    assert judgment.grounded is False
    assert "compliance" in judgment.reason
    assert usage == {"input_tokens": 50, "output_tokens": 10, "cached_tokens": 0}
    assert len(fake_llm.calls) == 1


def test_judge_grounding_fails_open_on_error():
    check = _make_check()
    result = EvaluationSchema(status="Pass", evidence="no gas activity found", source="schedule")

    judgment, usage = _judge_grounding(check, "evidence blob", result, _FakeErroringLLM(), {})

    assert judgment.grounded is True
    assert usage == {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}


def test_accumulate_usage_adds_call_and_tokens():
    totals = {"input_tokens": 5, "output_tokens": 2, "cached_tokens": 1, "llm_call_count": 1}

    _accumulate_usage(totals, {"input_tokens": 10, "output_tokens": 3, "cached_tokens": 0})

    assert totals == {"input_tokens": 15, "output_tokens": 5, "cached_tokens": 1, "llm_call_count": 2}


def test_retry_with_correction_includes_correction_and_returns_parsed():
    retried = EvaluationSchema(
        status="Pass", evidence="B1010 starts after ROW available.", source="B1010",
    )
    fake_llm = _FakeStructuredLLM([
        (retried, {"input_tokens": 80, "output_tokens": 15, "input_token_details": {"cache_read": 20}}),
    ])

    parsed, usage = _retry_with_correction(
        fake_llm,
        "original evidence\n\nCHECK: ROW Availability\ninstruction",
        "dates show compliance, not violation",
        {},
    )

    assert parsed is retried
    assert usage == {"input_tokens": 80, "output_tokens": 15, "cached_tokens": 20}
    sent_messages = fake_llm.calls[0]
    assert "dates show compliance" in sent_messages[-1].content


def test_retry_with_correction_returns_none_on_error():
    parsed, usage = _retry_with_correction(_FakeErroringLLM(), "msg", "reason", {})

    assert parsed is None
    assert usage == {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}


def _call_evaluate_one_check(check, structured_llm, structured_judge_llm, **overrides):
    kwargs = dict(
        schedule_facts="PRECOMPUTED FACTS: B1010 starts 2024-09-01.",
        narrative_text="",
        sp_search_fn=None,
        spec_search_fn=None,
        csm_search_fn=None,
        keymap_facts=None,
        estimate_facts=None,
        utility_plan_search_fn=None,
        deterministic=_DeterministicContext(),
        project_id="default",
        user_id=None,
        langfuse_handler=None,
    )
    kwargs.update(overrides)
    return _evaluate_one_check(check, structured_llm, structured_judge_llm, **kwargs)


def test_evaluate_one_check_grounded_pass_through():
    check = _make_check()
    original = EvaluationSchema(status="Fail", evidence="cited evidence", source="schedule")
    llm = _FakeStructuredLLM([(original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}})])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=True, reason="fine"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    result, usage = _call_evaluate_one_check(check, llm, judge)

    assert result.status == "Fail"
    assert result.evidence == "cited evidence"
    assert usage["llm_call_count"] == 2  # original + judge
    assert usage["judged"] == 1
    assert usage["ungrounded"] == 0
    assert usage["downgraded"] == 0
    assert len(judge.calls) == 1


def test_evaluate_one_check_grounded_pass_through_status_pass():
    """Same as test_evaluate_one_check_grounded_pass_through but for a
    status="Pass" original verdict — the judge condition is
    ``if result.status in ("Pass", "Fail")``, so Pass must be judged too,
    not just Fail."""
    check = _make_check()
    original = EvaluationSchema(status="Pass", evidence="cited evidence", source="schedule")
    llm = _FakeStructuredLLM([(original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}})])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=True, reason="fine"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    result, usage = _call_evaluate_one_check(check, llm, judge)

    assert result.status == "Pass"
    assert result.evidence == "cited evidence"
    assert usage["llm_call_count"] == 2  # original + judge
    assert usage["judged"] == 1
    assert usage["ungrounded"] == 0
    assert usage["downgraded"] == 0
    assert len(judge.calls) == 1


def test_evaluate_one_check_grounding_judge_can_be_disabled():
    """config.REVIEW_GROUNDING_JUDGE=False is the rollback toggle: it must
    skip the judge (and any retry) entirely, regardless of the original
    verdict, so the check falls back to its unjudged first answer."""
    check = _make_check()
    original = EvaluationSchema(status="Fail", evidence="cited evidence", source="schedule")
    llm = _FakeStructuredLLM([(original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}})])
    judge = _FakeStructuredLLM([])  # must not be called

    previous = config.REVIEW_GROUNDING_JUDGE
    config.REVIEW_GROUNDING_JUDGE = False
    try:
        result, usage = _call_evaluate_one_check(check, llm, judge)
    finally:
        config.REVIEW_GROUNDING_JUDGE = previous

    assert result.status == "Fail"
    assert result.evidence == "cited evidence"
    assert usage["llm_call_count"] == 1  # original only, no judge call
    assert usage["judged"] == 0
    assert len(judge.calls) == 0


def test_evaluate_one_check_ungrounded_retry_succeeds():
    check = _make_check()
    original = EvaluationSchema(status="Fail", evidence="wrong reading of dates", source="schedule")
    retried = EvaluationSchema(status="Pass", evidence="B1010 starts after ROW available", source="schedule")
    llm = _FakeStructuredLLM([
        (original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}}),
        (retried, {"input_tokens": 12, "output_tokens": 6, "input_token_details": {}}),
    ])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=False, reason="dates show compliance"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
        (GroundingJudgment(grounded=True, reason="now correct"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    result, usage = _call_evaluate_one_check(check, llm, judge)

    assert result.status == "Pass"
    assert result.evidence == "B1010 starts after ROW available"
    assert usage["llm_call_count"] == 4  # original + judge + retry + re-judge
    assert usage["judged"] == 1
    assert usage["ungrounded"] == 1
    assert usage["downgraded"] == 0


def test_evaluate_one_check_ungrounded_retry_still_fails():
    check = _make_check()
    original = EvaluationSchema(status="Fail", evidence="wrong reading", source="schedule")
    retried = EvaluationSchema(status="Fail", evidence="still wrong", source="schedule")
    llm = _FakeStructuredLLM([
        (original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}}),
        (retried, {"input_tokens": 12, "output_tokens": 6, "input_token_details": {}}),
    ])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=False, reason="dates show compliance"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
        (GroundingJudgment(grounded=False, reason="still contradicts dates"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    result, usage = _call_evaluate_one_check(check, llm, judge)

    assert result.status == "Missing"
    assert "still contradicts dates" in result.evidence
    assert result.source == "grounding verification failed"
    assert usage["llm_call_count"] == 4  # original + judge + retry + re-judge
    assert usage["judged"] == 1
    assert usage["ungrounded"] == 1
    assert usage["downgraded"] == 1


def test_evaluate_one_check_missing_status_skips_judge():
    check = _make_check()
    original = EvaluationSchema(status="Missing", evidence="no schedule data", source="no data provided")
    llm = _FakeStructuredLLM([(original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}})])
    judge = _FakeStructuredLLM([])  # must not be called

    result, usage = _call_evaluate_one_check(check, llm, judge)

    assert result.status == "Missing"
    assert usage["llm_call_count"] == 1
    assert usage["judged"] == 0
    assert usage["ungrounded"] == 0
    assert usage["downgraded"] == 0
    assert len(judge.calls) == 0


def test_evaluate_one_check_retry_call_fails_entirely():
    check = _make_check()
    original = EvaluationSchema(status="Fail", evidence="wrong reading", source="schedule")

    class _FirstCallThenError:
        def __init__(self, first_response):
            self._first = first_response
            self.calls = []

        def invoke(self, messages, config=None):
            self.calls.append(messages)
            if self._first is not None:
                parsed, usage_metadata = self._first
                self._first = None
                return {
                    "raw": SimpleNamespace(usage_metadata=usage_metadata),
                    "parsed": parsed,
                    "parsing_error": None,
                }
            raise RuntimeError("retry call failed")

    llm = _FirstCallThenError((original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}}))
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=False, reason="dates show compliance"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    result, usage = _call_evaluate_one_check(check, llm, judge)

    assert result.status == "Missing"
    assert result.source == "grounding verification failed"
    assert "dates show compliance" in result.evidence
    assert usage["llm_call_count"] == 3  # original + judge + failed retry attempt
    assert usage["judged"] == 1
    assert usage["ungrounded"] == 1
    assert usage["downgraded"] == 1
    assert len(judge.calls) == 1  # no re-judge, since the retry never produced an answer


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
