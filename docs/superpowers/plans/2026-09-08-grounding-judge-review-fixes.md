# Grounding Judge Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 5 highest-priority findings from the grounding-judge code review — a fail-open bug that can ship an unverified retry answer as "grounded," two missing try/except boundaries that can abort an entire review batch on one check's failure, an unconditional re-judge call, and the missing test coverage for the riskiest branch.

**Architecture:** All changes are confined to `backend/app/compliance/eval_engine.py`'s `_judge_grounding`/`_evaluate_one_check` functions and their test file. `_judge_grounding` changes its failure contract from a fabricated "always grounded" object to `None` (matching `_retry_with_correction`'s existing convention) — the two call sites then interpret `None` differently for their own situation (fail-open for the first judge, fail-closed for the re-judge), which is where the actual bug fix comes from.

**Tech Stack:** Python, pytest.

## Global Constraints

- `_judge_grounding`'s failure return changes from `(GroundingJudgment(grounded=True, ...), zeroed_usage)` to `(None, zeroed_usage)` — this is a breaking change to its return type (`Tuple[Optional[GroundingJudgment], Dict[str, int]]`), so every call site must be updated in the same change, not incrementally.
- No new fields on `GroundingJudgment`, `ReviewCheckResult`, or the usage dict. No new config flags. No new top-level helper functions (per the design spec's Non-goals — the simplification/reuse findings are explicitly deferred).
- Every existing test in `backend/tests/test_eval_engine.py` must continue to pass unchanged — this plan's design spec already traced each one by hand against the new control flow; the test steps below re-verify this empirically.
- Use `.venv/Scripts/python.exe` from the repo root for all Python/pytest commands (not `backend/.venv`).

Design reference: [docs/superpowers/specs/2026-09-08-grounding-judge-review-fixes-design.md](../specs/2026-09-08-grounding-judge-review-fixes-design.md)

---

### Task 1: Widen the existing try/except to cover evidence-gathering (finding #3)

**Files:**
- Modify: `backend/app/compliance/eval_engine.py:506-550` (inside `_evaluate_one_check`)

**Interfaces:**
- Produces: no new interface — this task only moves existing code inside an existing try block. `evidence` and `user_msg` (both already used later in the function, in the judge/retry block Task 2 touches) remain defined with the exact same names and meaning; they're just guaranteed to always be bound (pre-initialized to `""`) even on the new failure path.

- [ ] **Step 1: Confirm the current code matches this plan's assumptions**

Read `backend/app/compliance/eval_engine.py` lines 506-550 and confirm it matches:

```python
    evidence_parts: List[str] = []
    if "schedule" in sources:
        evidence_parts.append(schedule_facts)
    if "narrative" in sources:
        evidence_parts.append(narrative_text)
    if "sp" in sources:
        evidence_parts.append(sp_search_fn(check.instruction))
    if "keymap" in sources:
        evidence_parts.append(keymap_facts)
    if "estimate" in sources:
        evidence_parts.append(estimate_facts)
    if "spec" in sources:
        evidence_parts.append(spec_search_fn(check.instruction))
    if "csm" in sources:
        evidence_parts.append(csm_search_fn(check.instruction))
    if "utility_plan" in sources and utility_plan_search_fn is not None:
        evidence_parts.append(utility_plan_search_fn(check.instruction))
    evidence = "\n\n".join(evidence_parts) if evidence_parts else schedule_facts

    user_msg = f"{evidence}\n\nCHECK: {check.name}\n{check.instruction}"
    invoke_config = {
        "callbacks": [langfuse_handler] if langfuse_handler else [],
        "run_name": f"evaluate-check:{check.check_key}",
        "metadata": {
            "langfuse_session_id": project_id,
            "langfuse_user_id": user_id,
            "langfuse_tags": ["compliance_review", check.check_key],
        },
    }
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
```

If the line numbers or content have drifted, locate the equivalent block by searching for `evidence_parts: List[str] = []` and stop to reconcile before continuing — do not guess at the surrounding code.

- [ ] **Step 2: Write a failing test proving the current bug**

Add this test to `backend/tests/test_eval_engine.py`, anywhere before the `if __name__ == "__main__":` block at the bottom:

```python
def test_evaluate_one_check_search_fn_error_falls_back_to_missing():
    """Fix (review finding #3): a search_fn used to build evidence sits
    outside the existing try/except -- if it raises, the exception must be
    caught by the same handler the LLM-invoke failure already uses, not
    propagate out of _evaluate_one_check (which would abort the whole
    review batch, since evaluate_checks's completion loop has no try/except
    of its own)."""
    check = _make_check(source_files=["sp"])

    def _raising_search_fn(instruction):
        raise RuntimeError("simulated retrieval timeout")

    llm = _FakeStructuredLLM([])  # must not be reached
    judge = _FakeStructuredLLM([])  # must not be reached

    result, usage = _call_evaluate_one_check(
        check, llm, judge, sp_search_fn=_raising_search_fn,
    )

    assert result.status == "Missing"
    assert result.source == "error"
    assert usage["llm_call_count"] == 0
    assert len(llm.calls) == 0
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py::test_evaluate_one_check_search_fn_error_falls_back_to_missing -v`
Expected: FAIL — currently `_raising_search_fn`'s exception propagates uncaught out of `_evaluate_one_check` (it's called outside the try block), so the test raises `RuntimeError` instead of getting back a `Missing` result.

- [ ] **Step 4: Move evidence-gathering inside the try block**

In `backend/app/compliance/eval_engine.py`, replace the block from Step 1 with:

```python
    invoke_config = {
        "callbacks": [langfuse_handler] if langfuse_handler else [],
        "run_name": f"evaluate-check:{check.check_key}",
        "metadata": {
            "langfuse_session_id": project_id,
            "langfuse_user_id": user_id,
            "langfuse_tags": ["compliance_review", check.check_key],
        },
    }
    evidence = ""
    user_msg = ""
    try:
        evidence_parts: List[str] = []
        if "schedule" in sources:
            evidence_parts.append(schedule_facts)
        if "narrative" in sources:
            evidence_parts.append(narrative_text)
        if "sp" in sources:
            evidence_parts.append(sp_search_fn(check.instruction))
        if "keymap" in sources:
            evidence_parts.append(keymap_facts)
        if "estimate" in sources:
            evidence_parts.append(estimate_facts)
        if "spec" in sources:
            evidence_parts.append(spec_search_fn(check.instruction))
        if "csm" in sources:
            evidence_parts.append(csm_search_fn(check.instruction))
        if "utility_plan" in sources and utility_plan_search_fn is not None:
            evidence_parts.append(utility_plan_search_fn(check.instruction))
        evidence = "\n\n".join(evidence_parts) if evidence_parts else schedule_facts
        user_msg = f"{evidence}\n\nCHECK: {check.name}\n{check.instruction}"

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
```

(Only `evidence`/`user_msg` moved inside the `try`, plus their two pre-initializations added right before it — `invoke_config`'s construction and everything from `raw_result = ...` onward through the `except` block are unchanged from Step 1.)

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py::test_evaluate_one_check_search_fn_error_falls_back_to_missing -v`
Expected: PASS

- [ ] **Step 6: Run the full eval_engine test file to check for regressions**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py -v`
Expected: all tests pass (the existing 14 plus this new one = 15 passed)

- [ ] **Step 7: Commit**

```bash
git add backend/app/compliance/eval_engine.py backend/tests/test_eval_engine.py
git commit -m "fix: catch evidence-retrieval errors in the same handler as LLM-invoke failures"
```

---

### Task 2: Fail-closed re-judge, status-gated re-judge, and outer try/except (findings #1, #2, #4, #5)

**Files:**
- Modify: `backend/app/compliance/eval_engine.py:374-405` (`_judge_grounding`)
- Modify: `backend/app/compliance/eval_engine.py` (the judge/retry block inside `_evaluate_one_check` — locate by searching for `if config.REVIEW_GROUNDING_JUDGE and result.status in`)
- Modify: `backend/tests/test_eval_engine.py` (one new test)

**Interfaces:**
- Consumes: Task 1's `evidence`/`user_msg` (unchanged names/meaning).
- Produces: `_judge_grounding(...) -> Tuple[Optional[GroundingJudgment], Dict[str, int]]` — return type changes from `Tuple[GroundingJudgment, ...]` to `Tuple[Optional[GroundingJudgment], ...]`. No other function in the codebase calls `_judge_grounding` except `_evaluate_one_check` (both call sites are fixed in this same task) and the test file (updated in this same task) — confirmed via `grep -rn "_judge_grounding" backend/` before starting, to catch any other caller this plan doesn't know about.

- [ ] **Step 1: Confirm `_judge_grounding`'s current code matches this plan's assumptions**

Read `backend/app/compliance/eval_engine.py` lines 374-405 and confirm it matches:

```python
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

Also run `grep -rn "_judge_grounding" backend/` and confirm the only matches are: this function's own definition, its call sites inside `_evaluate_one_check` (fixed in Step 3 below), and references inside `backend/tests/test_eval_engine.py` (fixed in Step 5 below). If any other caller exists, stop and reconcile before continuing.

- [ ] **Step 2: Write the failing test proving the current bug**

Add this test to `backend/tests/test_eval_engine.py`, anywhere before the `if __name__ == "__main__":` block:

```python
def test_evaluate_one_check_ungrounded_retry_succeeds_but_rejudge_errors():
    """Fix (review finding #1): if the retry succeeds but the RE-judge call
    itself errors, the check must fail CLOSED (downgrade to Missing) --
    not silently accept the never-independently-verified retry answer as
    "grounded". Unlike the FIRST judge call (which fails open, trusting
    the original answer -- see test_judge_grounding_fails_open_on_error),
    the re-judge failing open would mean shipping an answer nothing ever
    actually verified."""
    check = _make_check()
    original = EvaluationSchema(status="Fail", evidence="wrong reading of dates", source="schedule")
    retried = EvaluationSchema(status="Pass", evidence="B1010 starts after ROW available", source="schedule")
    llm = _FakeStructuredLLM([
        (original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}}),
        (retried, {"input_tokens": 12, "output_tokens": 6, "input_token_details": {}}),
    ])
    # Only ONE queued response: the first judge call (ungrounded). The
    # re-judge call that follows has nothing queued, so _FakeStructuredLLM
    # raises on that second call (queue exhaustion) -- simulating the
    # re-judge call erroring.
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=False, reason="dates show compliance"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    result, usage = _call_evaluate_one_check(check, llm, judge)

    assert result.status == "Missing"
    assert result.source == "grounding verification failed"
    assert usage["llm_call_count"] == 4  # original + judge + retry + failed re-judge attempt
    assert usage["judged"] == 1
    assert usage["ungrounded"] == 1
    assert usage["downgraded"] == 1
    assert len(judge.calls) == 2  # first judge call, then the failed re-judge attempt
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py::test_evaluate_one_check_ungrounded_retry_succeeds_but_rejudge_errors -v`
Expected: FAIL — with the current code, `_judge_grounding`'s except block returns `GroundingJudgment(grounded=True, ...)` on the failed re-judge call, so `result.status` ends up `"Pass"` (the retry's status, silently accepted), not `"Missing"`.

- [ ] **Step 4: Fix `_judge_grounding` to return `None` on failure**

In `backend/app/compliance/eval_engine.py`, replace the function from Step 1 with:

```python
def _judge_grounding(
    check: CheckDef,
    evidence_blob: str,
    result: EvaluationSchema,
    structured_judge_llm: Runnable,
    invoke_config: dict,
) -> Tuple[Optional[GroundingJudgment], Dict[str, int]]:
    """Second-pass check: does `result`'s evidence actually support its
    status? Only called for Pass/Fail verdicts — Missing already means "not
    enough evidence," nothing to judge. Returns (None, zeroed usage) if the
    judge call itself errors -- callers decide what a failed judge call
    means for their own situation (see _evaluate_one_check: the first
    judge call fails open, treating None as "trust the original answer";
    the re-judge call fails closed, treating None as "not grounded")."""
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
        return None, {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
```

- [ ] **Step 5: Update the two call sites in `_evaluate_one_check`**

Find the block starting with `if config.REVIEW_GROUNDING_JUDGE and result.status in ("Pass", "Fail"):` inside `_evaluate_one_check`. It currently reads:

```python
    if config.REVIEW_GROUNDING_JUDGE and result.status in ("Pass", "Fail"):
        judge_config = {**invoke_config, "run_name": f"{invoke_config['run_name']}:judge"}
        judgment, judge_usage = _judge_grounding(check, evidence, result, structured_judge_llm, judge_config)
        _accumulate_usage(usage_totals, judge_usage)
        usage_totals["judged"] = 1

        if not judgment.grounded:
            logger.warning(
                "evaluate_checks: check %s judged ungrounded (%s)", check.check_key, judgment.reason,
            )
            usage_totals["ungrounded"] = 1
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
                # Note the asymmetry: if the FIRST judge call errors, _judge_grounding
                # fails open and we keep the original answer unverified (safe — it's
                # what we already had). If the RE-judge errors, it also fails open,
                # and we accept the RETRIED answer unverified here — a different
                # answer we've never actually verified. Both follow "an unreachable
                # judge never blocks a check," and accepting the correction is the
                # better of the two options, but it's worth being explicit that this
                # branch can be reached without the retry ever being judged.
                result = retried
            else:
                failure_reason = re_judgment.reason if re_judgment is not None else judgment.reason
                logger.warning(
                    "evaluate_checks: check %s downgraded to Missing after retry (%s)",
                    check.check_key, failure_reason,
                )
                usage_totals["downgraded"] = 1
                result = EvaluationSchema(
                    status="Missing",
                    evidence=f"Could not verify grounding after retry: {failure_reason}",
                    source="grounding verification failed",
                )
```

Replace the entire block with (this both fixes findings #1, #4, and wraps everything in a try/except for finding #2):

```python
    if config.REVIEW_GROUNDING_JUDGE and result.status in ("Pass", "Fail"):
        try:
            judge_config = {**invoke_config, "run_name": f"{invoke_config['run_name']}:judge"}
            judgment, judge_usage = _judge_grounding(check, evidence, result, structured_judge_llm, judge_config)
            _accumulate_usage(usage_totals, judge_usage)
            if judgment is not None:
                usage_totals["judged"] = 1

            if judgment is not None and not judgment.grounded:
                logger.warning(
                    "evaluate_checks: check %s judged ungrounded (%s)", check.check_key, judgment.reason,
                )
                usage_totals["ungrounded"] = 1
                retry_config = {**invoke_config, "run_name": f"{invoke_config['run_name']}:retry"}
                retried, retry_usage = _retry_with_correction(structured_llm, user_msg, judgment.reason, retry_config)
                _accumulate_usage(usage_totals, retry_usage)

                re_judgment = None
                if retried is not None and retried.status in ("Pass", "Fail"):
                    re_judge_config = {**invoke_config, "run_name": f"{invoke_config['run_name']}:rejudge"}
                    re_judgment, re_judge_usage = _judge_grounding(
                        check, evidence, retried, structured_judge_llm, re_judge_config,
                    )
                    _accumulate_usage(usage_totals, re_judge_usage)

                if retried is not None and retried.status not in ("Pass", "Fail"):
                    # Retry itself concluded Missing (or another non-judgeable
                    # status) -- nothing to ground-check, accept it directly,
                    # same as how a first-pass Missing answer skips judging.
                    result = retried
                elif retried is not None and re_judgment is not None and re_judgment.grounded:
                    result = retried
                else:
                    # Fail CLOSED here (unlike the first judge above): a
                    # retry answer that was never independently re-verified
                    # -- whether because the re-judge said "not grounded",
                    # the re-judge call itself errored, or the retry call
                    # itself failed -- must not ship as a confirmed result.
                    failure_reason = re_judgment.reason if re_judgment is not None else judgment.reason
                    logger.warning(
                        "evaluate_checks: check %s downgraded to Missing after retry (%s)",
                        check.check_key, failure_reason,
                    )
                    usage_totals["downgraded"] = 1
                    result = EvaluationSchema(
                        status="Missing",
                        evidence=f"Could not verify grounding after retry: {failure_reason}",
                        source="grounding verification failed",
                    )
        except Exception:
            logger.exception(
                "evaluate_checks: grounding judge/retry orchestration failed for check %s", check.check_key,
            )
            usage_totals["downgraded"] = 1
            result = EvaluationSchema(
                status="Missing",
                evidence="Could not verify grounding due to an internal error.",
                source="grounding verification failed",
            )
```

- [ ] **Step 6: Run the new test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py::test_evaluate_one_check_ungrounded_retry_succeeds_but_rejudge_errors -v`
Expected: PASS

- [ ] **Step 7: Run the full eval_engine test file to check for regressions**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py -v`
Expected: all tests pass. If any of the pre-existing tests fail, read the failure carefully against this plan's Global Constraints section before changing test expectations — the design was traced by hand to require zero changes to any existing test's assertions.

- [ ] **Step 8: Run the full backend suite to check for regressions outside this file**

Run: `.venv/Scripts/python.exe -m pytest backend/tests -q`
Expected: all tests pass (no other file calls `_judge_grounding` or `_evaluate_one_check` directly, per Step 1's grep, so this is a final confirmation, not expected to surface anything new).

- [ ] **Step 9: Commit**

```bash
git add backend/app/compliance/eval_engine.py backend/tests/test_eval_engine.py
git commit -m "fix: fail closed on re-judge errors, gate re-judge on retry status, guard judge/retry orchestration"
```
