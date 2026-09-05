# Compliance Review Grounding Judge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second-pass LLM judge to the compliance-review check pipeline so that a `Pass`/`Fail` verdict whose own cited evidence doesn't actually support it gets caught, retried once, and downgraded to an honest `Missing` rather than shipped as a confident wrong answer.

**Architecture:** After `_evaluate_one_check` in `eval_engine.py` gets a `Pass`/`Fail` result from the first LLM call, a second small structured-output call (`GroundingJudgment`) checks whether the evidence supports the status. If not, the original check is retried once with a corrective note and re-judged; if it's still ungrounded, the check reports `Missing` with an explanation. `Missing`-status results skip judging entirely (nothing to judge). No schema, frontend, or deterministic-check (`geo`/`cost_gap`/`edq_coverage`) changes.

**Tech Stack:** Python, LangChain (`.with_structured_output()`), Pydantic, pytest.

## Global Constraints

- No changes to `ReviewCheckResult`'s public schema (`status` stays `Literal["Pass", "Fail", "Missing"]`).
- No changes to the frontend, to deterministic checks, or to `ThreadPoolExecutor`/`REVIEW_CHECK_CONCURRENCY`.
- Exactly one retry on grounding failure — no loops.
- Judge failures fail open (`grounded=True`) — an unreachable judge must never block an otherwise-produced answer.
- Reuse the same `llm` already passed into `evaluate_checks` for the judge — no new model config.

Spec: `docs/superpowers/specs/2026-09-05-compliance-review-grounding-judge-design.md`

---

### Task 1: `GroundingJudgment` schema + judge helper

**Files:**
- Modify: `backend/app/models.py:108-111`
- Modify: `backend/app/compliance/eval_engine.py:54` (import), `:77-79` (prompt constants), `:325` (new helpers before `_evaluate_one_check`)
- Test: `backend/tests/test_eval_engine.py` (new file)

**Interfaces:**
- Produces: `GroundingJudgment(BaseModel)` in `app.models` — fields `grounded: bool`, `reason: str`.
- Produces: `_usage_from_raw(raw_result: dict) -> Dict[str, int]` in `eval_engine.py` — returns `{"input_tokens": int, "output_tokens": int, "cached_tokens": int}`.
- Produces: `_accumulate_usage(totals: Dict[str, int], call_usage: Dict[str, int]) -> None` in `eval_engine.py` — mutates `totals` in place: `+= 1` on `llm_call_count`, `+=` each of `call_usage`'s three fields.
- Produces: `_judge_grounding(check: CheckDef, evidence_blob: str, result: EvaluationSchema, structured_judge_llm: Runnable, invoke_config: dict) -> Tuple[GroundingJudgment, Dict[str, int]]` in `eval_engine.py`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_eval_engine.py`:

```python
"""Tests for the grounding-judge second-pass check in eval_engine.py."""

from types import SimpleNamespace

from app.compliance.catalog import CheckDef
from app.compliance.eval_engine import _accumulate_usage, _judge_grounding
from app.models import EvaluationSchema, GroundingJudgment


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py -v` (from the repo root — verified working invocation; `backend/.venv` is a stray venv missing pytest, do not use it)
Expected: FAIL with `ImportError: cannot import name '_accumulate_usage'` (`_judge_grounding` and `GroundingJudgment` don't exist yet either).

- [ ] **Step 3: Add `GroundingJudgment` to `app/models.py`**

Find this in `backend/app/models.py` (around line 102-108):

```python
class EvaluationSchema(BaseModel):
    """Structured output shape enforced via ``.with_structured_output()`` for
    each individual compliance-check LLM call."""

    status:   Literal["Pass", "Fail", "Missing"]
    evidence: str   # verbatim extraction or exact metric found
    source:   str   # page number, document name, or Task ID
```

Add immediately after it (before `class ReviewCheckResult`):

```python


class GroundingJudgment(BaseModel):
    """Structured output for the second-pass grounding judge — verifies that
    an ``EvaluationSchema`` result's evidence actually supports its status,
    catching self-contradictory verdicts a single LLM call can produce."""

    grounded: bool
    reason:   str   # brief explanation, especially when grounded=False
```

- [ ] **Step 4: Add the judge helpers to `eval_engine.py`**

Modify the import on line 54 of `backend/app/compliance/eval_engine.py`:

```python
from app.models import EvaluationSchema, ReviewCheckResult
```
becomes:
```python
from app.models import EvaluationSchema, GroundingJudgment, ReviewCheckResult
```

Find `_STATIC_SYSTEM_PROMPT` (lines 61-77) and add a second prompt constant right after its closing `"""`:

```python
_JUDGE_SYSTEM_PROMPT = """\
You are a strict fact-checker reviewing ONE compliance-check verdict for \
internal consistency. You will be shown the same evidence the original \
check saw, the rule being checked, and the verdict that was produced.

Your only job: does the cited evidence actually support the stated status? \
Look specifically for:
- A status that contradicts what the evidence itself shows (e.g. dates in \
the evidence indicate compliance, but status is "Fail").
- Evidence that does not appear to describe what it claims to (a quote \
attributed to a document that doesn't match the material shown).

grounded: true if the evidence genuinely supports the status. false if the \
status contradicts its own evidence, or the evidence looks fabricated.
reason: one sentence explaining your grounded/not-grounded call.
"""
```

Find `_evaluate_one_check`'s definition (`def _evaluate_one_check(` around line 326) and insert these two functions immediately **before** it (after `_DETERMINISTIC_EVALUATORS`'s closing `}`):

```python
def _usage_from_raw(raw_result: dict) -> Dict[str, int]:
    """Extract input/output/cached token counts from a structured-output
    call's raw response — shared by the original check call, the grounding
    judge, and the corrective retry, all of which return the same
    ``{"raw": ..., "parsed": ..., "parsing_error": ...}`` shape."""
    usage = getattr(raw_result.get("raw"), "usage_metadata", None) or {}
    return {
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "cached_tokens": (usage.get("input_token_details") or {}).get("cache_read", 0) or 0,
    }


def _accumulate_usage(totals: Dict[str, int], call_usage: Dict[str, int]) -> None:
    """Add one call's token usage into the running totals and bump the call
    count — shared by every call site in _evaluate_one_check (the original
    check, the judge, and the retry), so that logic isn't repeated at each."""
    totals["llm_call_count"] += 1
    totals["input_tokens"] += call_usage["input_tokens"]
    totals["output_tokens"] += call_usage["output_tokens"]
    totals["cached_tokens"] += call_usage["cached_tokens"]


def _judge_grounding(
    check: CheckDef,
    evidence_blob: str,
    result: EvaluationSchema,
    structured_judge_llm: Runnable,
    invoke_config: dict,
) -> Tuple[GroundingJudgment, Dict[str, int]]:
    """Second-pass check: does `result`'s evidence actually support its
    status? Only called for Pass/Fail verdicts — Missing already means "not
    enough evidence," nothing to judge. Fails open (grounded=True) if the
    judge call itself errors, so an unreachable judge never blocks an
    otherwise-reasonable answer."""
    judge_msg = (
        f"{evidence_blob}\n\nCHECK: {check.name}\n{check.instruction}\n\n"
        f"VERDICT TO REVIEW:\nstatus: {result.status}\n"
        f"evidence: {result.evidence}\nsource: {result.source}"
    )
    try:
        raw = structured_judge_llm.invoke(
            [SystemMessage(content=_JUDGE_SYSTEM_PROMPT), HumanMessage(content=judge_msg)],
            config=invoke_config,
        )
        judgment: Optional[GroundingJudgment] = raw.get("parsed")
        if judgment is None:
            raise ValueError(f"judge structured output parsing failed: {raw.get('parsing_error')}")
        return judgment, _usage_from_raw(raw)
    except Exception:
        logger.exception("evaluate_checks: grounding judge failed for check %s", check.check_key)
        return (
            GroundingJudgment(grounded=True, reason="Judge call failed; not blocking on it."),
            {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0},
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py -v` (from the repo root — verified working invocation; `backend/.venv` is a stray venv missing pytest, do not use it)
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/models.py backend/app/compliance/eval_engine.py backend/tests/test_eval_engine.py
git commit -m "feat: add GroundingJudgment schema and judge helper for compliance checks"
```

---

### Task 2: Corrective retry helper

**Files:**
- Modify: `backend/app/compliance/eval_engine.py` (add `_retry_with_correction`, right after `_judge_grounding` from Task 1)
- Test: `backend/tests/test_eval_engine.py` (append)

**Interfaces:**
- Consumes: `_usage_from_raw` from Task 1.
- Produces: `_retry_with_correction(structured_llm: Runnable, user_msg: str, correction: str, invoke_config: dict) -> Tuple[Optional[EvaluationSchema], Dict[str, int]]` in `eval_engine.py`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_eval_engine.py` (add `_retry_with_correction` to the existing import line from `app.compliance.eval_engine`):

```python
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
```

Update the import line at the top of the file:
```python
from app.compliance.eval_engine import _judge_grounding
```
becomes:
```python
from app.compliance.eval_engine import _judge_grounding, _retry_with_correction
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py -v` (from the repo root — verified working invocation; `backend/.venv` is a stray venv missing pytest, do not use it)
Expected: FAIL with `ImportError: cannot import name '_retry_with_correction'`

- [ ] **Step 3: Write the implementation**

Insert immediately after `_judge_grounding` in `backend/app/compliance/eval_engine.py`:

```python
def _retry_with_correction(
    structured_llm: Runnable,
    user_msg: str,
    correction: str,
    invoke_config: dict,
) -> Tuple[Optional[EvaluationSchema], Dict[str, int]]:
    """Re-run the original check once with a corrective note appended, after
    the first answer failed grounding verification. Returns (None, zeroed
    usage) if the retry call itself errors — the caller falls back to
    Missing either way, so a broken retry isn't a crash."""
    try:
        raw = structured_llm.invoke(
            [
                SystemMessage(content=_STATIC_SYSTEM_PROMPT),
                HumanMessage(content=user_msg),
                HumanMessage(
                    content=f"Your previous answer could not be verified: {correction} "
                    "Re-examine the evidence above and provide a corrected answer, "
                    "quoting only text that actually appears in it."
                ),
            ],
            config=invoke_config,
        )
        parsed: Optional[EvaluationSchema] = raw.get("parsed")
        if parsed is None:
            raise ValueError(f"retry structured output parsing failed: {raw.get('parsing_error')}")
        return parsed, _usage_from_raw(raw)
    except Exception:
        logger.exception("evaluate_checks: grounding retry failed")
        return None, {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py -v` (from the repo root — verified working invocation; `backend/.venv` is a stray venv missing pytest, do not use it)
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/compliance/eval_engine.py backend/tests/test_eval_engine.py
git commit -m "feat: add corrective retry helper for ungrounded compliance checks"
```

---

### Task 3: Wire the judge and retry into the check pipeline

**Files:**
- Modify: `backend/app/compliance/eval_engine.py:326-341` (`_evaluate_one_check` signature), `:415-439` (usage tracking + final return), `:488` (`evaluate_checks`, create `structured_judge_llm`), `:535-540` (`executor.submit` call)
- Test: `backend/tests/test_eval_engine.py` (append)

**Interfaces:**
- Consumes: `_judge_grounding` (Task 1), `_retry_with_correction` (Task 2), `_usage_from_raw` and `_accumulate_usage` (Task 1).
- Produces: `_evaluate_one_check` now takes a `structured_judge_llm: Runnable` parameter (inserted right after `structured_llm`); `evaluate_checks`'s public signature is unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_eval_engine.py`. Update the import line once more:
```python
from app.compliance.eval_engine import _judge_grounding, _retry_with_correction
```
becomes:
```python
from app.compliance.eval_engine import (
    _DeterministicContext,
    _evaluate_one_check,
    _judge_grounding,
    _retry_with_correction,
)
```

```python
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
    original = EvaluationSchema(status="Fail", evidence="fabricated", source="schedule")
    llm = _FakeStructuredLLM([(original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}})])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=True, reason="fine"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    result, usage = _call_evaluate_one_check(check, llm, judge)

    assert result.status == "Fail"
    assert result.evidence == "fabricated"
    assert usage["llm_call_count"] == 2  # original + judge
    assert len(judge.calls) == 1


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


def test_evaluate_one_check_missing_status_skips_judge():
    check = _make_check()
    original = EvaluationSchema(status="Missing", evidence="no schedule data", source="no data provided")
    llm = _FakeStructuredLLM([(original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}})])
    judge = _FakeStructuredLLM([])  # must not be called

    result, usage = _call_evaluate_one_check(check, llm, judge)

    assert result.status == "Missing"
    assert usage["llm_call_count"] == 1
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
    assert len(judge.calls) == 1  # no re-judge, since the retry never produced an answer
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py -v` (from the repo root — verified working invocation; `backend/.venv` is a stray venv missing pytest, do not use it)
Expected: FAIL — `_evaluate_one_check() missing 1 required positional argument: 'structured_judge_llm'` (or similar `TypeError`), since the function doesn't accept that parameter yet.

- [ ] **Step 3: Modify `_evaluate_one_check`'s signature**

Find (around line 326-341 of `backend/app/compliance/eval_engine.py`):

```python
def _evaluate_one_check(
    check: CheckDef,
    structured_llm: Runnable,
    schedule_facts: str,
```

Change to:

```python
def _evaluate_one_check(
    check: CheckDef,
    structured_llm: Runnable,
    structured_judge_llm: Runnable,
    schedule_facts: str,
```

- [ ] **Step 4: Replace the usage-tracking + final-return block**

Find this block (around lines 415-439):

```python
    try:
        raw_result = structured_llm.invoke(
            [SystemMessage(content=_STATIC_SYSTEM_PROMPT), HumanMessage(content=user_msg)],
            config=invoke_config,
        )
        usage_totals["llm_call_count"] = 1
        usage = getattr(raw_result.get("raw"), "usage_metadata", None) or {}
        usage_totals["input_tokens"] = usage.get("input_tokens", 0) or 0
        usage_totals["output_tokens"] = usage.get("output_tokens", 0) or 0
        usage_totals["cached_tokens"] = (usage.get("input_token_details") or {}).get("cache_read", 0) or 0

        result: Optional[EvaluationSchema] = raw_result.get("parsed")
        if result is None:
            raise ValueError(f"structured output parsing failed: {raw_result.get('parsing_error')}")
    except Exception:
        logger.exception("evaluate_checks: check %s failed", check.check_key)
        result = EvaluationSchema(
            status="Missing", evidence="Evaluation failed due to an internal error.",
            source="error",
        )

    return ReviewCheckResult(
        id=check.check_key, category=check.category, name=check.name,
        status=result.status, evidence=result.evidence, source=result.source,
    ), usage_totals
```

Replace it with:

```python
    try:
        raw_result = structured_llm.invoke(
            [SystemMessage(content=_STATIC_SYSTEM_PROMPT), HumanMessage(content=user_msg)],
            config=invoke_config,
        )
        _accumulate_usage(usage_totals, _usage_from_raw(raw_result))

        result: Optional[EvaluationSchema] = raw_result.get("parsed")
        if result is None:
            raise ValueError(f"structured output parsing failed: {raw_result.get('parsing_error')}")
    except Exception:
        logger.exception("evaluate_checks: check %s failed", check.check_key)
        result = EvaluationSchema(
            status="Missing", evidence="Evaluation failed due to an internal error.",
            source="error",
        )

    if result.status in ("Pass", "Fail"):
        judge_config = {**invoke_config, "run_name": f"{invoke_config['run_name']}:judge"}
        judgment, judge_usage = _judge_grounding(check, evidence, result, structured_judge_llm, judge_config)
        _accumulate_usage(usage_totals, judge_usage)

        if not judgment.grounded:
            retry_config = {**invoke_config, "run_name": f"{invoke_config['run_name']}:retry"}
            retried, retry_usage = _retry_with_correction(structured_llm, user_msg, judgment.reason, retry_config)
            _accumulate_usage(usage_totals, retry_usage)

            re_judgment = None
            if retried is not None:
                re_judge_config = {**invoke_config, "run_name": f"{invoke_config['run_name']}:rejudge"}
                re_judgment, re_judge_usage = _judge_grounding(
                    check, evidence, retried, structured_judge_llm, re_judge_config,
                )
                _accumulate_usage(usage_totals, re_judge_usage)

            if retried is not None and re_judgment is not None and re_judgment.grounded:
                result = retried
            else:
                failure_reason = re_judgment.reason if re_judgment is not None else judgment.reason
                result = EvaluationSchema(
                    status="Missing",
                    evidence=f"Could not verify grounding after retry: {failure_reason}",
                    source="grounding verification failed",
                )

    return ReviewCheckResult(
        id=check.check_key, category=check.category, name=check.name,
        status=result.status, evidence=result.evidence, source=result.source,
    ), usage_totals
```

- [ ] **Step 5: Wire `structured_judge_llm` through `evaluate_checks`**

Find (around line 488):

```python
    structured_llm = llm.with_structured_output(EvaluationSchema, include_raw=True)
```

Change to:

```python
    structured_llm = llm.with_structured_output(EvaluationSchema, include_raw=True)
    structured_judge_llm = llm.with_structured_output(GroundingJudgment, include_raw=True)
```

Find the `executor.submit` call (around lines 535-540):

```python
            future_to_index = {
                executor.submit(
                    _evaluate_one_check, check, structured_llm, schedule_facts, narrative_text,
                    sp_search_fn, spec_search_fn, csm_search_fn, keymap_facts, estimate_facts,
                    utility_plan_search_fn,
                    deterministic_ctx, project_id, user_id, langfuse_handler,
                ): i
                for i, check in enumerate(checks)
            }
```

Change to:

```python
            future_to_index = {
                executor.submit(
                    _evaluate_one_check, check, structured_llm, structured_judge_llm,
                    schedule_facts, narrative_text,
                    sp_search_fn, spec_search_fn, csm_search_fn, keymap_facts, estimate_facts,
                    utility_plan_search_fn,
                    deterministic_ctx, project_id, user_id, langfuse_handler,
                ): i
                for i, check in enumerate(checks)
            }
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py -v` (from the repo root — verified working invocation; `backend/.venv` is a stray venv missing pytest, do not use it)
Expected: PASS (10 tests total)

- [ ] **Step 7: Run the full backend test suite to check for regressions**

Run: `.venv/Scripts/python.exe -m pytest backend/tests -q` (from the repo root)
Expected: PASS — 60 passed (the 50 pre-existing tests in `test_chunk_store.py`, `test_edq.py`, `test_review_pdf_endpoint.py`, `test_session_citations.py`, plus the 10 new tests in `test_eval_engine.py`); none of the existing files exercise `_evaluate_one_check`, so none should be affected

- [ ] **Step 8: Commit**

```bash
git add backend/app/compliance/eval_engine.py backend/tests/test_eval_engine.py
git commit -m "feat: wire grounding judge and retry into the compliance check pipeline"
```

---

## Manual verification (not automated — needs live credentials)

The unit tests fake the LLM entirely, so they prove the retry/judge control flow is correct but can't prove the judge actually catches the two documented bugs against a real model. After merging, re-run a real review against the Route 49 project (the one behind `Route49_Schedule_Compliance_Comparison.docx`) and confirm:
- `row_availability` no longer scores `Fail` when the schedule dates are actually compliant.
- `water_interruption`'s evidence, if present, is a real quote from the ingested narrative (spot-check against `build_narrative_text`'s output for that project).

This isn't a plan task because it needs a live Neo4j-backed project and LLM credentials — it's the spec's "Success criteria" check, done by hand once the code is merged.
