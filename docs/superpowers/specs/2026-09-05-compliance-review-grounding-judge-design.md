# Compliance Review — Grounding Judge Design

## Background

An independent line-by-line audit of a Route 49 schedule-compliance review (`Route49_Schedule_Compliance_Comparison.docx`, 2026-09-04) compared the app's own `/api/review` output against a careful manual re-check of the same source documents. 37 of 60 comparable items agreed; 10 disagreed. Two of the disagreements are not differences of judgment — the app's verdict directly contradicts the evidence it itself cited:

- **ROW Availability**: app scored `Fail`, but its own quoted activity dates show the schedule starting *after* the ROW parcels become available — that's compliant. Should be `Pass`.
- **No Water Service Interruptions**: app's cited supporting quote, attributed to the designer's narrative, does not exist anywhere in the narrative document. Fabricated citation.

Both failures share a root cause: `_evaluate_one_check` in [eval_engine.py](../../../backend/app/compliance/eval_engine.py) makes one LLM call per check and returns whatever `status`/`evidence`/`source` comes back, with nothing verifying that the evidence actually supports the status. This is scoped to one fix for that root cause: a second-pass LLM judge that checks the first call's own answer before it's trusted.

A separate, complementary fix (a cheap deterministic check that the LLM's cited activity IDs/dates/section numbers literally appear in the source text — catches fabricated quotes for ~free, no LLM call) was discussed and deliberately deferred; it composes with this judge later by feeding into the same retry path, but is out of scope here.

## Goal

Add a second-pass LLM judge to the compliance-review LLM-check path: after `_evaluate_one_check` gets a `Pass` or `Fail` verdict, a second small LLM call checks whether the cited evidence actually supports that verdict. If not, retry the original check once with a corrective note; if the retry still doesn't hold up, report the check as `Missing` with an explanation rather than a confident but wrong answer.

## Non-goals

- The deterministic anchor-text grounding check (fabricated-quote detection without an LLM call) — separate follow-up.
- Catalog coverage gaps (missing notice-period check, narrative-vs-schedule consistency, staging tie-in gaps) — separate follow-up.
- Any change to `ReviewCheckResult`'s schema, the frontend, or deterministic checks (`geo`/`cost_gap`/`edq_coverage`) — none of these are touched.

## Design

**Scope of judging:** every check whose first-pass `status` is `Pass` or `Fail`. Not `Missing` — the schema has no separate `Warning` status (`Missing` already means "not enough evidence to claim anything," so there's nothing to judge). Both `Pass` and `Fail` are judged, not `Fail` alone: a wrong `Pass` (a real violation silently waved through) is at least as costly for a compliance tool as a wrong `Fail`, and costs the same one extra call either way.

**The judge call:** a second `.with_structured_output()` call against a new schema, `GroundingJudgment` (`grounded: bool`, `reason: str`), given the same evidence blob the original call saw, the check's rule text, and the first call's status/evidence/source. It answers one question: does the cited evidence actually support the stated status? Reuses the same `llm` already passed into `evaluate_checks` — no new model config.

**On `grounded=False`:** retry the *original* check once — same evidence, same instruction, plus one corrective message naming what the judge flagged. Re-judge the retry's answer. If the re-judge says `grounded=True`, use the retried result. If it's still `False` (or the retry call itself errors), report `status="Missing"` with an evidence string explaining that grounding couldn't be verified, `source="grounding verification failed"`. Exactly one retry — no loop.

**Failure-open on judge errors:** if the judge call itself throws (network error, parse failure), treat it as `grounded=True` and keep the original result — an unreachable judge should never block a check that already produced a reasonable answer.

**Cost shape:** +1 small judge call for every non-`Missing` check (roughly 50 of 60 on a typical review); +1 retry-generation call and +1 re-judge call only for the minority that fail the first judge. Runs inside the existing per-check worker thread — no change to `ThreadPoolExecutor`/`REVIEW_CHECK_CONCURRENCY`.

## Testing

`backend/tests/test_eval_engine.py` does not exist yet — this is its first coverage. A fake structured-LLM stub (queues canned `(parsed, usage_metadata)` responses per `.invoke()` call, since nothing in this codebase currently fakes a LangChain structured-output call) drives:

- Judge parses a response and extracts usage correctly.
- Judge fails open (`grounded=True`) when the underlying call raises.
- Retry helper includes the corrective note in its message and returns the parsed retry answer.
- Retry helper returns `None` when the retry call raises.
- End-to-end in `_evaluate_one_check`: grounded-on-first-try passes through unchanged; ungrounded-then-retry-succeeds swaps in the retried result; ungrounded-then-retry-still-fails downgrades to `Missing`; a `Missing`-status first answer skips the judge entirely (cost control).

## Success criteria

The two documented bugs (ROW Availability self-contradiction, fabricated water-interruption citation) no longer reproduce when re-run against the same Route 49 review inputs. No regression in checks that were already correct — the judge only overrides a verdict when it's actually ungrounded.
