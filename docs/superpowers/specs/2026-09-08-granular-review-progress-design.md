# Granular Review Progress Design

## Background

`POST /api/review` and `POST /api/review/{project_id}/rerun` already run
asynchronously: the endpoint returns immediately, the actual pipeline runs
via `BackgroundTasks`, and the frontend (`DocumentReview.tsx`) tracks
progress via an `EventSource` against `GET /api/review/{project_id}/status`
([2026-09-07-async-review-endpoint-design.md](2026-09-07-async-review-endpoint-design.md)).
That design deliberately deferred per-check progress granularity as a
"natural follow-up." This spec is that follow-up.

Two gaps exist today:

1. **The backend's existing coarse progress is never shown.** `_review_progress` already carries a `message` field on every update (`"Review queued…"`, etc.), but `DocumentReview.tsx`'s `onmessage` handler only reads `progress.message` for the terminal `'error'` case — every intermediate `'queued'`/`'running'` update is silently discarded. The loading UI instead shows a hardcoded string: `"Analyzing documents… this may take up to 1 min 30 seconds"` ([DocumentReview.tsx:961](../../../frontend/src/components/review/DocumentReview.tsx:961)), regardless of what the backend is actually doing.
2. **The backend has no granularity to show even if the frontend read it.** Between the pipeline starting and finishing, `_review_progress` only ever holds `status="running"` with whatever message was set when the background task began — there's no visibility into which pipeline stage is active, or how far through the 57-check catalog evaluation has gotten.

## Goal

Report real progress through a review's lifetime: which pipeline stage is
running (schedule seeding, narrative seeding, special provision
processing, key map/estimate extraction, utility plan processing), and a
live "N of 57 checks complete" counter during compliance evaluation —
surfaced on the existing single-line loading indicator in
`DocumentReview.tsx`.

## Non-goals

- Progress *within* a single check's LLM call (still atomic — a check is
  either pending or complete).
- Changing the rerun button's own inline label (`"Re-running…"`) — that's
  a compact button label, not a status panel; out of scope.
- A progress bar or percentage. Stage durations vary too much by which
  optional files were uploaded to make a meaningful percentage; the
  existing message-based SSE contract (free text set by the backend,
  displayed verbatim by the frontend) already covers this well for the
  `"queued"`/error states and extends naturally to more messages.
- Any change to `evaluate_checks`'s actual check-evaluation logic,
  concurrency model, or result shape — this is purely additive
  instrumentation around the existing `ThreadPoolExecutor`/`as_completed`
  loop.

## Design

### Backend: stage messages in `_run_review_pipeline`

`_run_review_pipeline` ([review.py:712](../../../backend/app/api/review.py:712))
already has `project_id` in scope and runs in the same process as
`_review_progress`/`_set_review_progress` (defined in the same module), so
no new parameter threading is needed for stage messages — the function
calls `_set_review_progress(project_id, status="running", message=...)`
directly at each stage boundary, matching the branch structure the code
already has:

**`reseed=True` (fresh review), in the order the code already runs them:**
1. Before `seed_schedule(...)` ([review.py:783](../../../backend/app/api/review.py:783)): `"Seeding schedule graph…"`
2. Before `seed_narrative(...)` ([review.py:791](../../../backend/app/api/review.py:791)): `"Seeding narrative graph…"`
3. Before the SP chunking block, only `if sp_bytes:` ([review.py:799](../../../backend/app/api/review.py:799)): `"Processing special provision…"`
4. Before `_extract_and_store_keymap(...)`, only `if keymap_bytes:` ([review.py:808](../../../backend/app/api/review.py:808)): `"Extracting key map…"`
5. Before `_extract_and_store_estimate(...)`, only `if estimate_bytes:` ([review.py:814](../../../backend/app/api/review.py:814)): `"Extracting engineer's estimate…"`
6. Before the utility plan loop, only `if utility_plan_bytes_list:` ([review.py:825](../../../backend/app/api/review.py:825)): `"Processing utility plans…"`

**`reseed=False` (rerun fast path, [review.py:848](../../../backend/app/api/review.py:848) onward)** — this path reads already-persisted data instead of re-parsing/re-extracting, so one message covers the whole branch:
7. At the top of the `else:` branch: `"Loading saved project data…"`

**Always, after the branch (covers `_seed_edq_items_if_needed`/`resolve_region`/`evaluate_cost_gap` — deterministic, no LLM calls, [review.py:882-896](../../../backend/app/api/review.py:882)):**
8. `"Preparing compliance checklist…"`

**Then, for the checklist itself:**
9. `"Running compliance checks (0/N)…"` immediately before calling `evaluate_checks`, then live-updated as each check completes (see below) up to `"Running compliance checks (N/N)…"`.

Each message is a plain `_set_review_progress(project_id, status="running", message="...")` call — same shape as the existing `"queued"`/`"error"` writes, so no changes to `_set_review_progress`, the SSE generator, or the status endpoint's contract.

### Backend: check-completion counter in `evaluate_checks`

`evaluate_checks` ([eval_engine.py:442](../../../backend/app/compliance/eval_engine.py:442)) gains one new optional parameter:

```python
def evaluate_checks(
    checks: List[CheckDef],
    graph: Neo4jGraph,
    llm: BaseChatModel,
    ...
    project_id: str = "default",
    user_id: Optional[str] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> List[ReviewCheckResult]:
```

Called once per completed check, inside the existing
`for future in as_completed(future_to_index):` loop
([eval_engine.py:543](../../../backend/app/compliance/eval_engine.py:543)) —
that loop already runs single-threaded in the calling thread (only the
check *evaluation* itself happens in worker threads), so incrementing a
local counter and invoking `on_progress(completed, len(checks))` there
needs no locking:

```python
    completed = 0
    for future in as_completed(future_to_index):
        i = future_to_index[future]
        result, usage = future.result()
        results_by_index[i] = result
        ...
        completed += 1
        if on_progress is not None:
            on_progress(completed, len(checks))
```

`eval_engine.py` stays decoupled from `review.py`'s progress-store
mechanism — it only knows about a generic callback, not `_review_progress`
directly. `_run_review_pipeline` passes the closure:

```python
    check_results = evaluate_checks(
        ...,
        on_progress=lambda done, total: _set_review_progress(
            project_id, status="running",
            message=f"Running compliance checks ({done}/{total})…",
        ),
    )
```

Since `evaluate_checks` is also called (indirectly, via `_run_review_pipeline`) from the rerun path, this counter works identically for both flows with no extra code.

**Known, accepted limitation:** the SSE status endpoint polls
`_review_progress` every 0.8s
([review.py generator, unchanged](../../../backend/app/api/review.py)).
A stage that completes faster than that gap (common for the whole
`reseed=False` fast path, or for the deterministic
edq/geo/cost-gap prep) may never be individually observed by a client —
only whichever message was most recently written at each 0.8s tick is
seen. This is a property of polling, not a bug, and doesn't affect
correctness: the terminal `ready`/`error` states are always delivered
before the stream closes.

### Frontend: `DocumentReview.tsx`

- Remove the hardcoded `"Analyzing documents… this may take up to 1 min 30 seconds"` string ([DocumentReview.tsx:961](../../../frontend/src/components/review/DocumentReview.tsx:961)).
- Add `const [loadingMessage, setLoadingMessage] = useState('Starting review…')` alongside the existing `isLoading`/`error` state. `runReview()` resets it to `'Starting review…'` when it starts (covering the gap between clicking and the `EventSource`'s first event — e.g. while a large raw file is still uploading).
- In `runReview()`'s `es.onmessage` handler, add a branch before the existing `'error'`/`'ready'` handling: whenever `progress.status === 'running'` (or `'queued'`) and `progress.message` is present, call `setLoadingMessage(progress.message)`. The existing `'error'` branch already does `setError(progress.message || ...)` — unaffected, it takes over messaging via `error` instead once that fires.
- Render `{loadingMessage}` in place of the old static text ([DocumentReview.tsx:960-962](../../../frontend/src/components/review/DocumentReview.tsx:960)) — the spinner SVG immediately before it is untouched.
- `handleRerun()` and its `"Re-running…"` button label are untouched (Non-goals).

## Testing

**Backend:**
- `backend/tests/test_eval_engine.py`: a new test asserting `on_progress` is called exactly `len(checks)` times, with strictly increasing `done` values ending at `(len(checks), len(checks))`, using the existing mocking pattern for `evaluate_checks`'s LLM/search-function dependencies.
- `backend/tests/test_review_endpoint.py` and `backend/tests/test_review_rerun_endpoint.py` (both already exercise `_run_review_pipeline` directly): extend existing pipeline tests (mocking `seed_schedule`/`seed_narrative`/extraction functions the way they already are) to additionally assert `_review_progress[project_id]["message"]` reflects the expected stage string at the expected point — at minimum, one test in `test_review_endpoint.py` for the `reseed=True` path hitting all six conditional/unconditional fresh-review stage messages, and one in `test_review_rerun_endpoint.py` for the `reseed=False` fast path's single `"Loading saved project data…"` message.
- Full backend suite must stay green; `evaluate_checks` callers that don't pass `on_progress` (if any exist outside `review.py`) must be unaffected by the new optional parameter's default of `None`.

**Frontend:** no unit-test harness exists for this component (confirmed absent earlier in this project's history — only `tsc --noEmit` and manual verification are available). Verify by:
1. `cd frontend && npx tsc --noEmit -p .` clean.
2. Manual run against the dev server (or a mocked `EventSource` in the browser console) confirming the loading text updates as different `status: "running"` messages arrive, and that clicking "Run Review" shows `"Starting review…"` before any message arrives.

## Success criteria

Submitting a review shows the loading indicator's text changing live
through each pipeline stage name and, during check evaluation, a
"Running compliance checks (X/57)" counter that increases as checks
complete — replacing today's single static, time-estimate string that
never changes regardless of what's actually happening.
