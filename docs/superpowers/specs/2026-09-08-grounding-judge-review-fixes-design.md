# Grounding Judge Review Fixes Design

## Background

An ultra code review of `2e4ffb4` (the merge that landed the grounding-judge
feature on `main`) found 15 findings across correctness, efficiency, and
cleanup. This spec covers the 5 highest-priority findings — real
correctness bugs plus one test-coverage gap — all confined to
`backend/app/compliance/eval_engine.py`'s judge/retry pipeline. The
remaining 10 findings (efficiency, simplification, reuse, and two
docs/config nits) are deliberately out of scope for this pass.

## The 5 findings this spec fixes

1. **Re-judge fail-open ships an unverified retry answer as "grounded".**
   `_judge_grounding` fails open (`grounded=True`) on any error, including
   the *re-judge* call (checking a corrective retry's answer). If the
   re-judge call itself errors, the code currently accepts the retry's
   answer as confirmed-grounded even though nothing ever actually verified
   it — the exact failure mode the whole feature exists to prevent.
2. **The judge/retry orchestration has no try/except of its own.** The two
   LLM-calling helpers (`_judge_grounding`, `_retry_with_correction`) each
   guard their own `.invoke()` call, but the glue code stitching them
   together (`_evaluate_one_check`, lines 552-596) isn't wrapped in
   anything — an unexpected exception there propagates out of the worker
   thread and aborts `evaluate_checks()`'s entire batch, discarding every
   other check's already-computed result.
3. **Evidence-retrieval calls sit outside the existing try/except.**
   `sp_search_fn`/`spec_search_fn`/`csm_search_fn`/`utility_plan_search_fn`
   (lines 511-522) run before the `try:` that guards the LLM call — a
   transient retrieval error has the same batch-aborting effect as #2.
   Pre-existing, but the grounding-judge feature raises the stakes (each
   check can now cost up to 4x more sunk LLM work before hitting this).
4. **Re-judge runs unconditionally, without checking the retry's status.**
   The first judge call is correctly gated to `status in ("Pass", "Fail")`
   (a Missing verdict has nothing to ground-check). The re-judge call has
   no equivalent gate — if a retry itself concludes `status="Missing"`,
   the code still spends an LLM call asking the judge to verify a Missing
   verdict, a question outside the judge's documented contract.
5. **No test covers "retry succeeds, but the re-judge call errors."** The
   existing 7 tests cover every other combination but not this one — the
   exact branch finding #1 lives in, and the one the code's own inline
   comment calls out as confusing enough to warrant a 7-line explanation.

## Design

### The core mechanism: `_judge_grounding` returns `None` on failure, not a fabricated verdict

Today, `_judge_grounding`'s `except` block returns
`GroundingJudgment(grounded=True, reason="Judge call failed; not blocking on it.")`
— a real-shaped object indistinguishable from an actual "grounded" verdict
at the call site. This is *why* finding #1 exists: there's no way for the
caller to tell "the judge ran and said grounded" apart from "the judge
couldn't run."

The fix changes `_judge_grounding` to return `(None, zeroed_usage)` on
failure — the exact same convention `_retry_with_correction` already uses.
Each of the two call sites then decides what a `None` means *for its own
situation*, which is where the two different failure postures (fail-open
for the first judge, fail-closed for the re-judge) actually come from —
not from two different implementations of `_judge_grounding`, just two
different `if` conditions reading its result:

- **First judge call:** the surrounding `if` becomes
  `if judgment is not None and not judgment.grounded:` — if the judge
  couldn't run, this is `False`, so the whole retry block is skipped and
  `result` stays the original, unverified-but-already-had-it answer.
  This is *the same behavior as today*, now expressed as a real `None`
  check instead of relying on a fabricated "always grounded" object.
- **Re-judge call:** no `if` needs a new fail-open branch at all. The
  final decision of whether to accept the retry checks
  `re_judgment is not None and re_judgment.grounded` — if the re-judge
  call failed, `re_judgment` is `None`, so this is `False` and control
  falls into the existing downgrade-to-`Missing` branch. Fail-closed,
  with no new code path — just the natural consequence of `None` no
  longer masquerading as `True`.

### Fix 4's gate folds into the same restructuring

The retry's status is checked once, right after the retry call returns,
before ever deciding whether to re-judge:

```python
if retried is not None and retried.status not in ("Pass", "Fail"):
    # Retry itself concluded Missing (or another non-judgeable status) --
    # nothing to ground-check, accept it directly, same as how a
    # first-pass Missing answer skips judging entirely.
    result = retried
elif retried is not None and re_judgment is not None and re_judgment.grounded:
    result = retried
else:
    # Fail CLOSED here (unlike the first judge above): a retry answer
    # that was never independently re-verified -- whether because the
    # re-judge said "not grounded", the re-judge call itself errored, or
    # the retry call itself failed -- must not ship as a confirmed
    # result.
    ...
```

The re-judge call itself only fires when
`retried is not None and retried.status in ("Pass", "Fail")` — folding
fix 4 into the same gate that fix 1 already needs, not a separate check.

### Fix 2 and 3: widen the existing try/except boundaries, don't add new ones

Both fixes extend an *existing* try/except rather than introducing a new
error-handling pattern:

- **Fix 3**: the evidence-gathering block (building `evidence_parts`,
  `evidence`, `user_msg`) moves *inside* the try block that already wraps
  the original LLM call, so a search-function error is caught by the same
  `except Exception:` that already falls back to
  `EvaluationSchema(status="Missing", evidence="Evaluation failed due to an internal error.", source="error")`.
  `evidence`/`user_msg` are pre-initialized to `""` before the try so
  they're always bound even on that failure path (defensive; nothing
  downstream currently reads them when `result.status != "Pass"/"Fail"`,
  but this removes any risk of a `NameError` if that ever changes).
- **Fix 2**: the entire judge/retry orchestration block (everything
  inside `if config.REVIEW_GROUNDING_JUDGE and result.status in (...)`)
  gets wrapped in its own try/except, falling back to the same
  `Missing`/`"grounding verification failed"` shape the existing downgrade
  path already produces, so a check that fails here degrades gracefully
  instead of aborting the whole review batch.

### A telemetry side effect, disclosed not hidden

Today, `usage_totals["judged"]` is set to `1` unconditionally once the
first judge call is *attempted*, regardless of whether it succeeded. With
this fix, `judged` is only set to `1` when `judgment is not None` — i.e.,
when the judge actually returned a real verdict. This is a deliberate,
small behavior change: the counter now means "we successfully judged this
check" rather than "we attempted to," which is arguably what the
telemetry was meant to measure in the first place (per the counter's own
purpose — observing the judge's *real* behavior in production). No
existing test breaks from this: every existing test scripts a
successfully-returning first judge call except the ones exercising a
totally different failure (a failed *retry*, not a failed *first judge*),
which still return a real judgment before that point.

### Test (fix 5)

One new test, modeled directly on the existing
`test_evaluate_one_check_ungrounded_retry_succeeds` test: same setup
(original ungrounded, retry produces a corrected Pass/Fail answer), but
the `judge` fixture's response queue only has ONE entry (the first judge
call's "ungrounded" verdict) — when the code calls `_judge_grounding` a
second time for the re-judge, `_FakeStructuredLLM.invoke()` pops from an
empty queue and raises `IndexError`, which `_judge_grounding`'s own
`except Exception:` catches and turns into `(None, zeroed_usage)` — this
is the *cheapest* way to simulate "the re-judge call errors" and reuses
`_FakeStructuredLLM`'s existing queue-exhaustion behavior rather than
adding a new fake LLM class. Asserts the check downgrades to `Missing`
with `source="grounding verification failed"`, and that
`llm_call_count == 4` (original + judge + retry + the failed re-judge
attempt, which still counts via `_accumulate_usage`'s unconditional
`+= 1` even though its own token usage is zeroed).

## Non-goals

- No change to `_retry_with_correction` — its `None`-on-failure contract
  is unchanged; it was already the pattern `_judge_grounding` is being
  brought in line with.
- No new fields, flags, or a third "arbiter" LLM call for the
  judge/re-judge-disagree case — considered during design and explicitly
  deferred; the re-judge's own verdict (or lack of one) remains the final
  word, matching the feature's existing 4-call-max shape.
- No change to `REVIEW_GROUNDING_JUDGE`'s default, its `os.getenv(...)`
  parsing, or its "instant disable" comment — those are separate findings
  (config/docs) explicitly out of scope for this pass.
- No extraction of a shared `_invoke_structured(...)` helper, no reuse of
  `user_msg` inside `_judge_grounding`, no derivation of `downgraded` from
  `results` after the fact — all three are real, separately-verified
  findings from the same review, but they're simplification/reuse cleanup
  the user explicitly chose to leave for a later pass, not correctness
  fixes.

## Testing

- The 1 new test described above (fix 5).
- Full backend suite must stay green — every existing test in
  `backend/tests/test_eval_engine.py` was traced by hand against the new
  control flow during design (documented per-test in this spec's
  reasoning above) and confirmed to still pass unchanged.
- No frontend changes — this is backend-only.

## Success criteria

`.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py -v`
shows all existing tests plus the 1 new test passing, with the new test
proving a re-judge call failure now downgrades to `Missing` instead of
silently shipping the retry's unverified answer.
