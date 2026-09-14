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

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.compliance.catalog import CheckDef  # noqa: E402
from app.compliance.eval_engine import (  # noqa: E402
    EvidenceCandidate,
    _DeterministicContext,
    _accumulate_usage,
    _derive_status,
    _evaluate_one_check,
    _judge_grounding,
    _retry_with_correction,
    _validate_items,
    build_narrative_text,
    evaluate_checks,
)
from app.config import config  # noqa: E402
from app.models import EvaluationSchema, GroundingJudgment, ReviewCheckResult, ReviewCitation  # noqa: E402


def _fake_checks(n: int) -> list[CheckDef]:
    return [
        CheckDef(check_key=f"c{i}", category="Cat", name=f"Check {i}", instruction="do it")
        for i in range(n)
    ]


def test_build_narrative_text_tags_chunks_with_page_pdf():
    graph = MagicMock()
    graph.query.return_value = [
        {"id": "n1", "heading": "Site Overview", "text": "Utilities cross Route 49.", "pagePdf": 3},
        {"id": "n2", "heading": None, "text": "No further utility work planned.", "pagePdf": 4},
    ]

    text, candidates = build_narrative_text(graph, project_id="proj1")

    assert "[cite:narrative-0]" in text
    assert "[cite:narrative-1]" in text
    assert "Utilities cross Route 49." in text
    assert candidates["narrative-0"] == EvidenceCandidate(
        kind="private", doc_type="narrative", label="Site Overview", page_pdf=3,
    )
    assert candidates["narrative-1"].label == "Narrative"
    assert candidates["narrative-1"].page_pdf == 4


def test_build_narrative_text_empty_when_no_chunks():
    graph = MagicMock()
    graph.query.return_value = []

    text, candidates = build_narrative_text(graph)

    assert text == "DESIGNER'S NARRATIVE: not provided."
    assert candidates == {}


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
         patch("app.compliance.eval_engine.build_narrative_text", return_value=("", {})), \
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
         patch("app.compliance.eval_engine.build_narrative_text", return_value=("", {})), \
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


def test_derive_status_fail_when_breaching_items_non_empty():
    result = EvaluationSchema(
        considered_items=["B1010", "B1020"], breaching_items=["B1020"],
        evidence="e", source="s",
    )
    assert _derive_status(result) == "Fail"


def test_derive_status_pass_when_breaching_items_empty():
    result = EvaluationSchema(considered_items=["B1010"], breaching_items=[], evidence="e", source="s")
    assert _derive_status(result) == "Pass"


def test_derive_status_pass_on_supported_absence():
    # No railroad on this project -- considered_items is legitimately empty
    # too (nothing to consider), and that must still be Pass, not Missing.
    result = EvaluationSchema(considered_items=[], breaching_items=[], evidence="no railroad found", source="s")
    assert _derive_status(result) == "Pass"


def test_derive_status_missing_on_insufficient_evidence():
    # The material the rule needs was not retrieved / is absent -- that is
    # needs-review, never a green Pass. Ignored once breaching_items is set.
    result = EvaluationSchema(insufficient_evidence=True, evidence="108.12 was not retrieved", source="s")
    assert _derive_status(result) == "Missing"
    result = EvaluationSchema(
        considered_items=["B1010"], breaching_items=["B1010"], insufficient_evidence=True,
        evidence="e", source="s",
    )
    assert _derive_status(result) == "Fail"


def test_validate_items_requires_whole_token_and_tolerates_section_zero_padding():
    # "M1" must not pass by hiding inside "M100"; "105.07.01" must match an
    # SP that prints the section as "105.07.1".
    evidence = "MILESTONES: M100 | Advertise ...\n[cite:sp-0] 105.07.1 UTILITY WORK ..."
    rejected = EvaluationSchema(considered_items=["M1"], evidence="e", source="s")
    assert "M1" in (_validate_items(rejected, evidence) or "")
    accepted = EvaluationSchema(considered_items=["105.07.01", "M100"], evidence="e", source="s")
    assert _validate_items(accepted, evidence) is None


def test_validate_items_passes_when_all_items_appear_in_evidence():
    result = EvaluationSchema(
        considered_items=["B1010", "B1020"], breaching_items=["B1020"],
        evidence="e", source="s",
    )
    assert _validate_items(result, "roster: B1010 ...; B1020 ...") is None


def test_validate_items_rejects_breaching_item_not_in_considered_items():
    result = EvaluationSchema(
        considered_items=["B1010"], breaching_items=["B1020"], evidence="e", source="s",
    )
    error = _validate_items(result, "roster: B1010 ...; B1020 ...")
    assert error is not None
    assert "B1020" in error


def test_validate_items_rejects_item_not_present_in_evidence():
    # The no_paving_winter-shaped failure: a real activity ID that simply
    # never appears anywhere in the evidence the check actually received.
    result = EvaluationSchema(
        considered_items=["Z9999"], breaching_items=["Z9999"], evidence="e", source="s",
    )
    error = _validate_items(result, "roster: B1010 ...; B1020 ...")
    assert error is not None
    assert "Z9999" in error


def test_validate_items_is_case_insensitive():
    result = EvaluationSchema(
        considered_items=["gas main"], breaching_items=["gas main"], evidence="e", source="s",
    )
    assert _validate_items(result, "Activity: Install GAS MAIN and Roadway Features") is None


def test_judge_grounding_parses_response():
    check = _make_check()
    result = EvaluationSchema(
        considered_items=["B1010"],
        breaching_items=["B1010"],
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
    result = EvaluationSchema(evidence="no gas activity found", source="schedule")

    judgment, usage = _judge_grounding(check, "evidence blob", result, _FakeErroringLLM(), {})

    assert judgment.grounded is True
    assert usage == {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}


def test_accumulate_usage_adds_call_and_tokens():
    totals = {"input_tokens": 5, "output_tokens": 2, "cached_tokens": 1, "llm_call_count": 1}

    _accumulate_usage(totals, {"input_tokens": 10, "output_tokens": 3, "cached_tokens": 0})

    assert totals == {"input_tokens": 15, "output_tokens": 5, "cached_tokens": 1, "llm_call_count": 2}


def test_retry_with_correction_includes_correction_and_returns_parsed():
    retried = EvaluationSchema(
        evidence="B1010 starts after ROW available.", source="B1010",
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
        narrative_result=("", {}),
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


def test_evaluate_one_check_item_validation_retry_recovers():
    """A hallucinated ID (never appears in the schedule evidence) is caught
    mechanically before the judge ever runs, and a corrected retry that
    only names real items is accepted."""
    check = _make_check()
    original = EvaluationSchema(
        considered_items=["Z9999"], breaching_items=["Z9999"],
        evidence="Z9999 breaches the rule", source="schedule",
    )
    retried = EvaluationSchema(
        considered_items=["B1010"], breaching_items=[],
        evidence="B1010 is compliant", source="schedule",
    )
    llm = _FakeStructuredLLM([
        (original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}}),
        (retried, {"input_tokens": 12, "output_tokens": 6, "input_token_details": {}}),
    ])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=True, reason="fine"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    result, usage = _call_evaluate_one_check(check, llm, judge)

    assert result.status == "Pass"
    assert result.evidence == "B1010 is compliant"
    assert usage["llm_call_count"] == 3  # original + item-validation retry + judge
    assert len(judge.calls) == 1  # judge only ever sees the corrected answer


def test_evaluate_one_check_item_validation_retry_still_invalid():
    """If the retry STILL hallucinates, the items are proven absent from the
    evidence -- that must not drive a red Fail (or a green Pass). Report
    Missing / needs review with the note, and never spend a judge call on
    items already known not to exist."""
    check = _make_check()
    original = EvaluationSchema(
        considered_items=["Z9999"], breaching_items=["Z9999"],
        evidence="Z9999 breaches the rule", source="schedule",
    )
    retried = EvaluationSchema(
        considered_items=["Z8888"], breaching_items=["Z8888"],
        evidence="Z8888 breaches the rule", source="schedule",
    )
    llm = _FakeStructuredLLM([
        (original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}}),
        (retried, {"input_tokens": 12, "output_tokens": 6, "input_token_details": {}}),
    ])
    judge = _FakeStructuredLLM([])  # must not be reached -- item validation never cleared

    result, usage = _call_evaluate_one_check(check, llm, judge)

    assert result.status == "Missing"  # fabricated breaching_items never become a Fail
    assert "could not be fully verified" in result.evidence
    assert usage["llm_call_count"] == 2  # original + item-validation retry, no judge
    assert len(judge.calls) == 0


def test_evaluate_one_check_item_retry_call_error_is_flagged_missing():
    """The fabrication was already proven by _validate_items; a retry call
    that errors must not ship the original unmarked (to a judge told not to
    re-check existence) -- flag it and report Missing."""
    check = _make_check()
    original = EvaluationSchema(
        considered_items=["Z9999"], breaching_items=["Z9999"],
        evidence="Z9999 breaches the rule", source="schedule",
    )

    class _FirstCallThenError:
        def __init__(self, first):
            self._first = first
            self.calls = []

        def invoke(self, messages, config=None):
            self.calls.append(messages)
            if self._first is not None:
                parsed, usage_metadata = self._first
                self._first = None
                return {"raw": SimpleNamespace(usage_metadata=usage_metadata), "parsed": parsed, "parsing_error": None}
            raise RuntimeError("retry call failed")

    llm = _FirstCallThenError((original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}}))
    judge = _FakeStructuredLLM([])  # must not be reached

    result, usage = _call_evaluate_one_check(check, llm, judge)

    assert result.status == "Missing"
    assert "Z9999" in result.evidence and "could not be verified" in result.evidence
    assert usage["llm_call_count"] == 2  # original + failed retry attempt
    assert len(judge.calls) == 0


def test_evaluate_one_check_grounding_retry_that_fabricates_keeps_original_flagged():
    """A judge-driven retry that introduces an ID absent from the evidence is
    not a usable correction: the re-judge (told existence was already
    checked) must never see it. Keep the validated original, flagged."""
    check = _make_check()
    original = EvaluationSchema(
        considered_items=["B1010"], breaching_items=["B1010"],
        evidence="B1010 starts before ROW available", source="schedule",
    )
    retried = EvaluationSchema(
        considered_items=["Z9999"], breaching_items=["Z9999"],
        evidence="Z9999 breaches", source="schedule",
    )
    llm = _FakeStructuredLLM([
        (original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}}),
        (retried, {"input_tokens": 12, "output_tokens": 6, "input_token_details": {}}),
    ])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=False, reason="dates show compliance"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])  # only one entry: a re-judge of the fabricated retry must never happen

    result, usage = _call_evaluate_one_check(check, llm, judge)

    assert result.status == "Fail"
    assert "B1010 starts before ROW available" in result.evidence
    assert "Z9999" not in result.evidence
    assert "could not be independently confirmed" in result.evidence
    assert usage["downgraded"] == 1
    assert len(judge.calls) == 1


def test_evaluate_one_check_ungrounded_pass_after_double_failure_is_missing():
    """An empty breaching list the judge rejected twice carries no
    information -- unlike an ungrounded Fail, whose item list is often still
    right -- so it must render as Missing, not a green COMPLIANT."""
    check = _make_check()
    original = EvaluationSchema(evidence="searched, nothing found", source="schedule")
    retried = EvaluationSchema(evidence="still nothing found", source="schedule")
    llm = _FakeStructuredLLM([
        (original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}}),
        (retried, {"input_tokens": 12, "output_tokens": 6, "input_token_details": {}}),
    ])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=False, reason="the evidence lists B1010 which breaches"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
        (GroundingJudgment(grounded=False, reason="still ignores B1010"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    result, usage = _call_evaluate_one_check(check, llm, judge)

    assert result.status == "Missing"
    assert "still ignores B1010" in result.evidence
    assert usage["downgraded"] == 1


def test_evaluate_one_check_deterministic_runs_before_missing_sources_gate():
    """A deterministic check listing an optional upload in source_files must
    still get its computed verdict when that upload is absent -- the gate
    only describes what the LLM path needs."""
    from app.compliance.date_rule import DateRuleResult

    check = _make_check(
        check_key="award_to_construction", check_type="date_rule",
        source_files=["schedule", "keymap", "estimate"],
    )
    ctx = _DeterministicContext(date_rule={
        "award_to_construction": DateRuleResult(False, "20 business day(s) (minimum 40 for State)."),
    })
    llm = _FakeStructuredLLM([])  # must not be reached
    judge = _FakeStructuredLLM([])

    result, usage = _call_evaluate_one_check(check, llm, judge, deterministic=ctx)

    assert result.status == "Fail"
    assert "minimum 40" in result.evidence
    assert usage["llm_call_count"] == 0


def test_evaluate_one_check_grounded_pass_through():
    check = _make_check()
    original = EvaluationSchema(
        considered_items=["B1010"], breaching_items=["B1010"],
        evidence="cited evidence", source="schedule",
    )
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
    Pass verdict (empty breaching_items) -- the judge runs unconditionally
    whenever REVIEW_GROUNDING_JUDGE is on, so a Pass must be judged too,
    not just a Fail."""
    check = _make_check()
    original = EvaluationSchema(evidence="cited evidence", source="schedule")
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
    original = EvaluationSchema(
        considered_items=["B1010"], breaching_items=["B1010"],
        evidence="cited evidence", source="schedule",
    )
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
    original = EvaluationSchema(
        considered_items=["B1010"], breaching_items=["B1010"],
        evidence="wrong reading of dates", source="schedule",
    )
    retried = EvaluationSchema(
        considered_items=["B1010"], breaching_items=[],
        evidence="B1010 starts after ROW available", source="schedule",
    )
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
    """Double-failure (judge rejects both the original and the retry) no
    longer collapses to Missing -- it keeps the retry's own mechanically-
    derived status (still Fail here, since its breaching_items is
    non-empty) and appends an automated note to the evidence instead of
    discarding the finding into an amber "Missing" pill."""
    check = _make_check()
    original = EvaluationSchema(
        considered_items=["B1010"], breaching_items=["B1010"],
        evidence="wrong reading", source="schedule",
    )
    retried = EvaluationSchema(
        considered_items=["B1010"], breaching_items=["B1010"],
        evidence="still wrong", source="schedule",
    )
    llm = _FakeStructuredLLM([
        (original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}}),
        (retried, {"input_tokens": 12, "output_tokens": 6, "input_token_details": {}}),
    ])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=False, reason="dates show compliance"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
        (GroundingJudgment(grounded=False, reason="still contradicts dates"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    result, usage = _call_evaluate_one_check(check, llm, judge)

    assert result.status == "Fail"
    assert "still wrong" in result.evidence
    assert "still contradicts dates" in result.evidence
    assert "recommend human review" in result.evidence.lower()
    assert result.source == "schedule"
    assert usage["llm_call_count"] == 4  # original + judge + retry + re-judge
    assert usage["judged"] == 1
    assert usage["ungrounded"] == 1
    assert usage["downgraded"] == 1


def test_evaluate_one_check_primary_call_error_returns_missing_without_judge():
    """Missing is no longer something the model can author directly (see
    EvaluationSchema's docstring) -- the only LLM-path source of Missing
    left is the primary call itself failing/erroring, which short-circuits
    before the judge, item validation, or any retry ever run."""
    check = _make_check()
    llm = _FakeErroringLLM()
    judge = _FakeStructuredLLM([])  # must not be called

    result, usage = _call_evaluate_one_check(check, llm, judge)

    assert result.status == "Missing"
    assert "internal error" in result.evidence
    assert usage["llm_call_count"] == 0
    assert usage["judged"] == 0
    assert usage["ungrounded"] == 0
    assert usage["downgraded"] == 0
    assert len(judge.calls) == 0


def test_evaluate_one_check_retry_call_fails_entirely():
    check = _make_check()
    original = EvaluationSchema(
        considered_items=["B1010"], breaching_items=["B1010"],
        evidence="wrong reading", source="schedule",
    )

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

    # Retry call itself errored -- keeps the original (unretried) result,
    # same fail-open posture as an unreachable judge, flagged rather than
    # collapsed to Missing.
    assert result.status == "Fail"
    assert result.source == "schedule"
    assert "wrong reading" in result.evidence
    assert "dates show compliance" in result.evidence
    assert usage["llm_call_count"] == 3  # original + judge + failed retry attempt
    assert usage["judged"] == 1
    assert usage["ungrounded"] == 1
    assert usage["downgraded"] == 1
    assert len(judge.calls) == 1  # no re-judge, since the retry never produced an answer


def test_evaluate_one_check_builds_verified_citation_from_matched_tag():
    check = _make_check(source_files=["sp"])
    original = EvaluationSchema(
        considered_items=["Gas work"], breaching_items=["Gas work"],
        evidence="SP section bars gas work in July.", source="SP 105.03",
        cited_chunk_ids=["sp-0"],
    )
    llm = _FakeStructuredLLM([(original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}})])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=True, reason="fine"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    def sp_search_fn(query, top_k=8):
        return "[cite:sp-0] Gas work is prohibited in July.", {
            "sp-0": EvidenceCandidate(kind="private", doc_type="special_provision", label="Special Provision", page_pdf=7),
        }, False  # anchor_missing -- unrelated to this check, no anchor named

    result, _ = _call_evaluate_one_check(check, llm, judge, sp_search_fn=sp_search_fn)

    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.verified is True
    assert citation.page_pdf == 7
    assert citation.doc_type == "special_provision"


def test_evaluate_one_check_flags_unmatched_citation_tag():
    check = _make_check(source_files=["sp"])
    original = EvaluationSchema(
        considered_items=["Gas work"], breaching_items=["Gas work"],
        evidence="claims to quote SP text", source="SP 105.03",
        cited_chunk_ids=["sp-99"],
    )
    llm = _FakeStructuredLLM([(original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}})])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=True, reason="fine"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    def sp_search_fn(query, top_k=8):
        return "[cite:sp-0] Gas work is prohibited in July.", {
            "sp-0": EvidenceCandidate(kind="private", doc_type="special_provision", label="Special Provision", page_pdf=7),
        }, False  # anchor_missing -- unrelated to this check, no anchor named

    result, _ = _call_evaluate_one_check(check, llm, judge, sp_search_fn=sp_search_fn)

    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.verified is False
    assert citation.page_pdf is None


def test_evaluate_one_check_adds_automatic_keymap_and_estimate_citations():
    check = _make_check(source_files=["keymap", "estimate", "schedule"])
    original = EvaluationSchema(evidence="utility crosses I-195", source="key map")
    llm = _FakeStructuredLLM([(original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}})])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=True, reason="fine"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    result, _ = _call_evaluate_one_check(
        check, llm, judge, keymap_facts="KEY MAP FACTS: ...", estimate_facts="PROJECT COST FACTS: ...",
    )

    doc_types = {c.doc_type for c in result.citations}
    assert doc_types == {"key_map", "estimate"}
    assert all(c.verified for c in result.citations)
    assert all(c.page_pdf == 1 for c in result.citations)


def test_evaluate_one_check_grounding_unresolved_keeps_citations_from_final_answer():
    """Double-failure keeps the retried answer (including its own
    cited_chunk_ids) rather than discarding citations into a bare Missing
    result -- the automatic keymap citation is still added on top, as for
    any other result."""
    check = _make_check(source_files=["sp", "keymap"])
    original = EvaluationSchema(
        considered_items=["Some SP text"], breaching_items=["Some SP text"],
        evidence="wrong reading", source="schedule", cited_chunk_ids=["sp-0"],
    )
    retried = EvaluationSchema(
        considered_items=["Some SP text"], breaching_items=["Some SP text"],
        evidence="still wrong", source="schedule", cited_chunk_ids=["sp-0"],
    )
    llm = _FakeStructuredLLM([
        (original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}}),
        (retried, {"input_tokens": 12, "output_tokens": 6, "input_token_details": {}}),
    ])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=False, reason="dates show compliance"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
        (GroundingJudgment(grounded=False, reason="still contradicts dates"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    def sp_search_fn(query, top_k=8):
        return "[cite:sp-0] Some SP text.", {
            "sp-0": EvidenceCandidate(kind="private", doc_type="special_provision", label="Special Provision", page_pdf=1),
        }, False  # anchor_missing -- unrelated to this check, no anchor named

    result, _ = _call_evaluate_one_check(
        check, llm, judge, sp_search_fn=sp_search_fn, keymap_facts="KEY MAP FACTS: ...",
    )

    assert result.status == "Fail"
    assert "recommend human review" in result.evidence.lower()
    doc_types = {c.doc_type for c in result.citations}
    assert doc_types == {"special_provision", "key_map"}


def test_both_sp_closures_return_identical_passages_for_one_query():
    """The fresh-review closure applied no similarity floor and the rerun
    closure applied 0.2, so the same check retrieved differently depending on
    which code path ran. One implementation, one result.

    Builds both app.api.review SP closures over the same backing data (the
    _FakeDB/_chunk fakes from test_check_retrieval.py) and runs one
    identical, anchored query through each.
    """
    from app.api.review import _build_sp_search_fn, _build_sp_search_fn_from_supabase
    from test_check_retrieval import _FakeDB, _chunk, _INSTRUCTION

    # One anchor-matching chunk (pinned by both closures) plus two plain
    # chunks that only the dense leg can surface.
    pinned_chunk = _chunk(
        "t", section="105.05", tables=["TABLE 105.05-1"], body="Pinned SP text.", chunk_index=0,
    )
    vector_chunk1 = _chunk("v1", body="Vector match one.")
    vector_chunk2 = _chunk("v2", body="Vector match two.")

    # A zero query embedding makes _cosine() return 0.0 for every candidate
    # (norm(query)=0 => denom=0), so the in-process closure's ranking is a
    # stable sort that preserves list order -- the same order _FakeDB hands
    # back verbatim (it does not do real vector math either). No keyword
    # rows on either side, so the RRF fuse the Supabase closure performs
    # never has a keyword-driven reordering to diverge on.
    embed_fn = lambda q: [0.0, 0.0, 0.0]  # noqa: E731
    embeddings = SimpleNamespace(embed_query=embed_fn)

    db = _FakeDB(pinned=[pinned_chunk], vector=[vector_chunk1, vector_chunk2], keyword=())
    supabase_fn = _build_sp_search_fn_from_supabase(db, embeddings, "p1")

    # pinned_chunk is deliberately NOT first here (unlike the Supabase
    # closure's separate `pinned=` fixture above, which is pin-only by
    # construction and so can't prove anything about ordering). If the
    # in-process closure's pinning were disabled, a stable sort over this
    # list would leave pinned_chunk last -- so this fixture only produces
    # sp-0/sp-1/sp-2 order matching the Supabase closure when pinning
    # actually moves it to the front.
    sp_chunks = [vector_chunk1, vector_chunk2, pinned_chunk]
    sp_vectors = [[0.0, 0.0, 0.0]] * 3
    memory_fn = _build_sp_search_fn(sp_chunks, sp_vectors, embeddings)

    text_a, _candidates_a, anchor_missing_a = supabase_fn(_INSTRUCTION, top_k=8)
    text_b, _candidates_b, anchor_missing_b = memory_fn(_INSTRUCTION, top_k=8)

    tags_a = re.findall(r"\[cite:(sp-\d+)\]", text_a)
    tags_b = re.findall(r"\[cite:(sp-\d+)\]", text_b)

    assert tags_a == ["sp-0", "sp-1", "sp-2"]  # sanity: not vacuously equal
    assert text_a == text_b
    assert tags_a == tags_b
    assert len(tags_a) == len(tags_b)
    assert anchor_missing_a == anchor_missing_b


def test_both_sp_closures_agree_when_instruction_names_parent_section():
    """FINDING 2's dot-bounded prefix pinning (commit d47ee99) landed in
    check_retrieval.pin_by_anchors -- the Supabase-backed closure -- but was
    never extended to the in-process closure's own pin test. That is the
    same class of drift Task 8 exists to eliminate, and the parity test
    above didn't catch it because its one fixture only exercised an exact
    section_id match. A check naming the PARENT section "105.07" must pin a
    chunk headed by the CHILD "105.07.02" identically on both paths.
    """
    from app.api.review import _build_sp_search_fn, _build_sp_search_fn_from_supabase
    from test_check_retrieval import _FakeDB, _chunk

    parent_instruction = (
        "Special Provisions 105.07 CONFORMITY WITH AND DEVIATIONS FROM PLANS "
        "AND STAKES: \"deviations require written approval\".\n\n"
        "Confirm deviation approvals are documented."
    )

    child_chunk = _chunk("t", section="105.07.02", body="Child section text.", chunk_index=0)
    vector_chunk1 = _chunk("v1", body="Vector match one.")
    vector_chunk2 = _chunk("v2", body="Vector match two.")

    embed_fn = lambda q: [0.0, 0.0, 0.0]  # noqa: E731
    embeddings = SimpleNamespace(embed_query=embed_fn)

    # The Supabase closure trusts its DB filter (already dot-bounded, per
    # commit d47ee99) to have matched the child row -- handed back here as
    # if the real `metadata->>section_id.like.105.07.*` filter found it.
    db = _FakeDB(pinned=[child_chunk], vector=[vector_chunk1, vector_chunk2], keyword=())
    supabase_fn = _build_sp_search_fn_from_supabase(db, embeddings, "p1")

    # child_chunk deliberately not first: if the in-process closure's
    # dot-bounded matching is missing (the actual bug), a stable sort over
    # this list leaves it last instead of pinned to the front.
    sp_chunks = [vector_chunk1, vector_chunk2, child_chunk]
    sp_vectors = [[0.0, 0.0, 0.0]] * 3
    memory_fn = _build_sp_search_fn(sp_chunks, sp_vectors, embeddings)

    text_a, _candidates_a, anchor_missing_a = supabase_fn(parent_instruction, top_k=8)
    text_b, _candidates_b, anchor_missing_b = memory_fn(parent_instruction, top_k=8)

    tags_a = re.findall(r"\[cite:(sp-\d+)\]", text_a)
    tags_b = re.findall(r"\[cite:(sp-\d+)\]", text_b)

    assert tags_a == ["sp-0", "sp-1", "sp-2"]  # sanity: not vacuously equal
    assert text_a == text_b
    assert tags_a == tags_b
    assert len(tags_a) == len(tags_b)
    assert anchor_missing_a == anchor_missing_b


def test_sp_anchor_missing_sole_source_reports_missing_with_no_llm_call():
    """anchor_missing=True means the project HAS section metadata and the
    check's own named anchor (TABLE 105.05-1) matched nothing in the Special
    Provision -- and here "sp" is this check's ONLY evidence source, so
    there is nothing else left to consult. Must short-circuit to Missing
    without ever calling the LLM, must name the anchor so a reviewer knows
    what to look for, and must NOT claim the clause is absent from the
    project (only that it wasn't found in the Special Provision) -- unlike a
    project ingested before section-aware chunking (which must degrade
    silently, see test_sp_anchor_missing_false_behaves_exactly_as_before),
    and unlike a multi-source check (see the next test)."""
    check = _make_check(
        check_key="working_drawing_review_time", source_files=["sp"],
        instruction=(
            "Category is set by Table 105.05-1 - but the Special Provisions "
            "often REPLACE that table."
        ),
    )

    def sp_search_fn(query, top_k=8):
        return "[cite:sp-0] unrelated clause text", {
            "sp-0": EvidenceCandidate(kind="private", doc_type="special_provision", label="Special Provision"),
        }, True  # anchor_missing

    llm = _FakeStructuredLLM([])
    result, usage = _call_evaluate_one_check(
        check, llm, _FakeStructuredLLM([]), sp_search_fn=sp_search_fn,
    )

    assert result.status == "Missing"
    assert "105.05" in result.evidence or "TABLE 105.05-1" in result.evidence
    assert "not found in the Special Provision" in result.evidence
    assert "absent from" not in result.evidence.lower()
    assert usage["llm_call_count"] == 0
    # usage accumulates only after invoke() returns, so a raising invoke (the
    # empty response queue's .pop(0) would raise IndexError) also leaves
    # llm_call_count at 0 -- that alone doesn't prove invoke was never
    # reached. .calls is appended before the pop, so this is what actually
    # proves it.
    assert len(llm.calls) == 0


def test_sp_anchor_missing_multi_source_reaches_other_sources():
    """anchor_missing=True but "sp" is NOT this check's sole source (it also
    names "spec") -- must NOT short-circuit. The spec source must still be
    queried and reach the LLM, with a note about the missing SP anchor
    folded into the evidence rather than a terminal Missing verdict. This is
    the FINDING 1 regression case: nearby_projects' 105.06 anchor is absent
    from the Special Provision but the check also names "spec", whose base
    Standard Specifications text can still answer it."""
    check = _make_check(
        check_key="nearby_projects", source_files=["sp", "spec"],
        instruction="Look in: Special Provisions 105.06; Standard Specifications 105.06.",
    )
    original = EvaluationSchema(
        considered_items=[], breaching_items=[],
        evidence="105.06 base form, no adjacent work named", source="spec 105.06",
    )
    llm = _FakeStructuredLLM([(original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}})])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=True, reason="fine"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    def sp_search_fn(query, top_k=8):
        return "[cite:sp-0] unrelated clause text", {
            "sp-0": EvidenceCandidate(kind="private", doc_type="special_provision", label="Special Provision"),
        }, True  # anchor_missing

    spec_calls = []

    def spec_search_fn(query):
        spec_calls.append(query)
        return "[cite:specs-0] 105.06 Cooperation with Others (base form).", {
            "specs-0": EvidenceCandidate(kind="public", doc_type="spec", label="Standard Specifications"),
        }

    result, usage = _call_evaluate_one_check(
        check, llm, judge, sp_search_fn=sp_search_fn, spec_search_fn=spec_search_fn,
    )

    # spec was actually queried -- the other source was reached, not skipped.
    assert len(spec_calls) == 1
    # the LLM (and judge) ran normally -- no terminal short-circuit.
    assert len(llm.calls) == 1
    assert usage["llm_call_count"] == 2  # original + judge
    assert result.status == "Pass"
    # the evidence the LLM actually saw carries a note about the missing SP
    # anchor, softly worded (not claiming project-wide absence), plus the
    # spec text that resolves the check.
    sent_evidence = llm.calls[0][1].content
    assert "not found in the Special Provision" in sent_evidence
    assert "does not mean the clause is absent from the project" in sent_evidence
    assert "105.06 Cooperation with Others (base form)" in sent_evidence


def test_sp_anchor_missing_false_behaves_exactly_as_before():
    """anchor_missing=False -- whether because no anchor was named or the
    project predates section-aware chunking and pinning couldn't work at all
    -- must not skip anything: evidence still goes to the LLM exactly as it
    did before this task."""
    check = _make_check(source_files=["sp", "schedule"])
    original = EvaluationSchema(
        considered_items=["B1010"], breaching_items=[],
        evidence="Nothing relevant found", source="schedule",
    )
    llm = _FakeStructuredLLM([(original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}})])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=True, reason="fine"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    def sp_search_fn(query, top_k=8):
        return "[cite:sp-0] some SP text", {
            "sp-0": EvidenceCandidate(kind="private", doc_type="special_provision", label="Special Provision"),
        }, False  # anchor_missing

    result, usage = _call_evaluate_one_check(check, llm, judge, sp_search_fn=sp_search_fn)

    assert result.status == "Pass"
    assert usage["llm_call_count"] == 2  # original + judge
    assert len(llm.calls) == 1


def test_spec_search_fn_two_tuple_path_is_unaffected():
    """spec/csm (and every other non-SP source) still return the plain
    CitedSearch two-tuple -- this task's three-tuple change must stay
    confined to the SP path, per this task's own scope note."""
    check = _make_check(source_files=["spec"])
    original = EvaluationSchema(evidence="Spec text found", source="spec 105.03")
    llm = _FakeStructuredLLM([(original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}})])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=True, reason="fine"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    def spec_search_fn(query):
        return "[cite:specs-0] Standard Spec text.", {
            "specs-0": EvidenceCandidate(kind="public", doc_type="spec", label="Standard Specifications"),
        }

    result, usage = _call_evaluate_one_check(check, llm, judge, spec_search_fn=spec_search_fn)

    assert result.status == "Pass"
    assert usage["llm_call_count"] == 2  # original + judge


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
