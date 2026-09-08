# Granular Review Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface real progress during a compliance review — which pipeline stage is running, and a live "N of 57 checks complete" counter — on the frontend's loading indicator, replacing today's static, unchanging text.

**Architecture:** Two additive backend changes (a completion-count callback threaded into `evaluate_checks`'s existing `ThreadPoolExecutor`/`as_completed` loop, and direct `_set_review_progress(...)` calls at each stage boundary already present in `_run_review_pipeline`), consumed by one frontend change (a new `loadingMessage` state tracked from every non-terminal SSE event instead of the current hardcoded string).

**Tech Stack:** Python/FastAPI backend (`backend/app/compliance/eval_engine.py`, `backend/app/api/review.py`), Next.js/React frontend (`frontend/src/components/review/DocumentReview.tsx`), pytest for backend tests.

## Global Constraints

- Every new progress message uses the exact wording specified in this plan — these strings are user-facing copy, not internal identifiers, and must match verbatim.
- No changes to `evaluate_checks`'s check-evaluation logic, concurrency model, or return type — this is purely additive instrumentation.
- No changes to `_set_review_progress`, the SSE generator, or the `/api/review/{project_id}/status` endpoint's contract — new messages use the exact same `_set_review_progress(project_id, status="running", message=...)` shape already in use.
- `handleRerun()`'s inline `"Re-running…"` button label in `DocumentReview.tsx` is explicitly out of scope — do not touch it.
- Full backend suite (`.venv/Scripts/python.exe -m pytest backend/tests -q`, run from the repo root) and `cd frontend && npx tsc --noEmit -p .` must both stay clean after every task.

Design reference: [docs/superpowers/specs/2026-09-08-granular-review-progress-design.md](../specs/2026-09-08-granular-review-progress-design.md)

---

### Task 1: Check-completion counter in `evaluate_checks`

**Files:**
- Modify: `backend/app/compliance/eval_engine.py:442-457` (signature), `:533-550` (the `ThreadPoolExecutor`/`as_completed` loop)
- Create: `backend/tests/test_eval_engine.py`

**Interfaces:**
- Produces: `evaluate_checks(..., on_progress: Optional[Callable[[int, int], None]] = None)` — an optional callback invoked once per completed check, with `(completed_count, total_count)`, called from the same thread that calls `evaluate_checks` (never from a worker thread — no locking needed). Task 2 consumes this exact parameter name and signature.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_eval_engine.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py -v`
Expected: both tests FAIL with `TypeError: evaluate_checks() got an unexpected keyword argument 'on_progress'`

- [ ] **Step 3: Add the `on_progress` parameter and invoke it in the completion loop**

In `backend/app/compliance/eval_engine.py`, change the `evaluate_checks` signature (lines 442-457) from:

```python
def evaluate_checks(
    checks: List[CheckDef],
    graph: Neo4jGraph,
    llm: BaseChatModel,
    sp_search_fn: Optional[Callable[[str], str]] = None,
    spec_search_fn: Optional[Callable[[str], str]] = None,
    csm_search_fn: Optional[Callable[[str], str]] = None,
    keymap_facts: Optional[str] = None,
    keymap_geo: Optional[RegionResult] = None,
    estimate_facts: Optional[str] = None,
    cost_gap: Optional[CostGapResult] = None,
    edq_coverage: Optional[EdqCoverageResult] = None,
    utility_plan_search_fn: Optional[Callable[[str], str]] = None,
    project_id: str = "default",
    user_id: Optional[str] = None,
) -> List[ReviewCheckResult]:
```

to:

```python
def evaluate_checks(
    checks: List[CheckDef],
    graph: Neo4jGraph,
    llm: BaseChatModel,
    sp_search_fn: Optional[Callable[[str], str]] = None,
    spec_search_fn: Optional[Callable[[str], str]] = None,
    csm_search_fn: Optional[Callable[[str], str]] = None,
    keymap_facts: Optional[str] = None,
    keymap_geo: Optional[RegionResult] = None,
    estimate_facts: Optional[str] = None,
    cost_gap: Optional[CostGapResult] = None,
    edq_coverage: Optional[EdqCoverageResult] = None,
    utility_plan_search_fn: Optional[Callable[[str], str]] = None,
    project_id: str = "default",
    user_id: Optional[str] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> List[ReviewCheckResult]:
```

Also add one sentence to the docstring (right after the existing "Checks are dispatched to a thread pool..." paragraph):

```python
    If given, ``on_progress(completed_count, total_count)`` is called once
    per finished check, from the same thread that called ``evaluate_checks``
    (the completion loop below, never a worker thread) -- safe to use for
    UI progress reporting without any locking.
```

Then change the completion loop (lines 543-550) from:

```python
            for future in as_completed(future_to_index):
                i = future_to_index[future]
                result, usage = future.result()
                results_by_index[i] = result
                total_input_tokens += usage["input_tokens"]
                total_output_tokens += usage["output_tokens"]
                total_cached_tokens += usage["cached_tokens"]
                llm_call_count += usage["llm_call_count"]
```

to:

```python
            completed = 0
            for future in as_completed(future_to_index):
                i = future_to_index[future]
                result, usage = future.result()
                results_by_index[i] = result
                total_input_tokens += usage["input_tokens"]
                total_output_tokens += usage["output_tokens"]
                total_cached_tokens += usage["cached_tokens"]
                llm_call_count += usage["llm_call_count"]
                completed += 1
                if on_progress is not None:
                    on_progress(completed, len(checks))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py -v`
Expected: both tests PASS

- [ ] **Step 5: Run the full backend suite to check for regressions**

Run: `.venv/Scripts/python.exe -m pytest backend/tests -q`
Expected: all tests pass (no existing caller of `evaluate_checks` passes positional args past `user_id`, so the new parameter's default of `None` is backward-compatible)

- [ ] **Step 6: Commit**

```bash
git add backend/app/compliance/eval_engine.py backend/tests/test_eval_engine.py
git commit -m "feat: add on_progress callback to evaluate_checks for live check-completion counts"
```

---

### Task 2: Pipeline stage messages in `_run_review_pipeline`

**Files:**
- Modify: `backend/app/api/review.py:766-969` (`_run_review_pipeline`'s body)
- Modify: `backend/tests/test_review_endpoint.py` (add tests + imports)
- Modify: `backend/tests/test_review_rerun_endpoint.py` (add tests + imports)

**Interfaces:**
- Consumes: `evaluate_checks(..., on_progress=...)` from Task 1 — exact parameter name.
- Produces: no new public interface — this task only adds `_set_review_progress(project_id, status="running", message=...)` calls at fixed points inside the existing `_run_review_pipeline` function, using `_set_review_progress` and `BUILTIN_CHECKS`, both already imported/defined in `review.py`.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_review_endpoint.py`, change the import line:

```python
from unittest.mock import patch
```

to:

```python
import contextlib
from unittest.mock import MagicMock, patch
```

and change:

```python
from app.api.review import review_endpoint, _review_progress  # noqa: E402
```

to:

```python
from app.api.review import review_endpoint, _review_progress, _run_review_pipeline  # noqa: E402
```

Then add this helper and these two tests anywhere before the `if __name__ == "__main__":` block at the bottom of `backend/tests/test_review_endpoint.py`:

```python
def _common_pipeline_patches(graph=None):
    """Context-manager stack patching every external dependency
    `_run_review_pipeline` touches to a safe no-op, for tests that only
    care about the sequence of `_set_review_progress` calls it makes.
    `graph` lets a test control what `get_neo4j()` returns (e.g. to
    control the reseed=False fast-path detection query)."""
    graph = graph if graph is not None else MagicMock()
    stack = contextlib.ExitStack()
    stack.enter_context(patch("app.api.review.get_neo4j", return_value=graph))
    stack.enter_context(patch("app.api.review.get_db", return_value=MagicMock()))
    stack.enter_context(patch("app.api.review.OpenAIEmbeddings", return_value=MagicMock()))
    stack.enter_context(patch("app.api.review.ChatOpenAI", return_value=MagicMock()))
    stack.enter_context(patch("app.api.review.ChatAnthropic", return_value=MagicMock()))
    stack.enter_context(patch(
        "app.api.review.parse_xer_all",
        return_value={"activities": [], "calendars": [], "project": {}},
    ))
    stack.enter_context(patch("app.api.review.seed_schedule"))
    stack.enter_context(patch("app.api.review._bytes_to_pdf_pages", return_value=[]))
    stack.enter_context(patch("app.api.review.chunk_narrative", return_value=[]))
    stack.enter_context(patch("app.api.review.seed_narrative"))
    stack.enter_context(patch("app.api.review._seed_edq_items_if_needed"))
    stack.enter_context(patch("app.api.review.evaluate_edq_coverage", return_value=None))
    stack.enter_context(patch("app.api.review._build_static_doc_search_fn", return_value=None))
    stack.enter_context(patch("app.api.review.evaluate_checks", return_value=[]))
    return stack


def test_run_review_pipeline_fresh_review_reports_unconditional_stage_messages():
    _review_progress.clear()
    try:
        with _common_pipeline_patches(), \
             patch("app.api.review._set_review_progress") as mock_progress:
            _run_review_pipeline(
                schedule_bytes=b"", narrative_bytes=b"", sp_bytes=None,
                keymap_bytes=None, estimate_bytes=None, selected_checks=None,
                project_id="p1", reseed=True, user_id="user-1",
            )

        messages = [c.kwargs.get("message") for c in mock_progress.call_args_list]
        assert messages == [
            "Seeding schedule graph…",
            "Seeding narrative graph…",
            "Preparing compliance checklist…",
            "Running compliance checks (0/57)…",
        ]
    finally:
        _review_progress.clear()


def test_run_review_pipeline_fresh_review_reports_conditional_stage_messages():
    _review_progress.clear()
    try:
        with _common_pipeline_patches(), \
             patch("app.api.review._bytes_to_sp_chunks", return_value=[]), \
             patch("app.api.review._extract_and_store_keymap", return_value=None), \
             patch("app.api.review._extract_and_store_estimate", return_value=None), \
             patch("app.api.review.extract_utility_plan", return_value=None), \
             patch("app.api.review._set_review_progress") as mock_progress:
            _run_review_pipeline(
                schedule_bytes=b"", narrative_bytes=b"", sp_bytes=b"sp",
                keymap_bytes=b"km", estimate_bytes=b"est", selected_checks=None,
                project_id="p1", reseed=True, user_id="user-1",
                utility_plan_bytes_list=[b"up"],
            )

        messages = [c.kwargs.get("message") for c in mock_progress.call_args_list]
        assert messages == [
            "Seeding schedule graph…",
            "Seeding narrative graph…",
            "Processing special provision…",
            "Extracting key map…",
            "Extracting engineer's estimate…",
            "Processing utility plans…",
            "Preparing compliance checklist…",
            "Running compliance checks (0/57)…",
        ]
    finally:
        _review_progress.clear()
```

In `backend/tests/test_review_rerun_endpoint.py`, change the import line:

```python
from unittest.mock import patch
```

to:

```python
import contextlib
from unittest.mock import MagicMock, patch
```

and change:

```python
from app.api.review import (  # noqa: E402
    _review_progress,
    _run_review_rerun_background,
    review_rerun_endpoint,
)
```

to:

```python
from app.api.review import (  # noqa: E402
    _review_progress,
    _run_review_pipeline,
    _run_review_rerun_background,
    review_rerun_endpoint,
)
```

Then add this test anywhere before the `if __name__ == "__main__":` block:

```python
def test_run_review_pipeline_rerun_reports_single_fast_path_message():
    """reseed=False, project already seeded (graph.query returns a nonzero
    count so it doesn't fall back to a full reseed) -- the fast path is a
    single stage, not six conditional ones. No manual _review_progress
    clear needed -- this file's setup_function (above) already clears it
    before every test."""
    graph = MagicMock()
    graph.query.return_value = [{"c": 1}]
    with patch("app.api.review.get_neo4j", return_value=graph), \
         patch("app.api.review.get_db", return_value=MagicMock()), \
         patch("app.api.review.OpenAIEmbeddings", return_value=MagicMock()), \
         patch("app.api.review.ChatOpenAI", return_value=MagicMock()), \
         patch("app.api.review.ChatAnthropic", return_value=MagicMock()), \
         patch("app.api.review._read_project_summary", return_value=("Proj", 10)), \
         patch("app.api.review._build_sp_search_fn_from_supabase", return_value=None), \
         patch("app.api.review._build_utility_plan_search_fn_from_supabase", return_value=None), \
         patch("app.api.review._read_keymap_extraction_from_supabase", return_value=None), \
         patch("app.api.review._read_estimate_extraction_from_supabase", return_value=None), \
         patch("app.api.review._seed_edq_items_if_needed"), \
         patch("app.api.review.evaluate_edq_coverage", return_value=None), \
         patch("app.api.review._build_static_doc_search_fn", return_value=None), \
         patch("app.api.review.evaluate_checks", return_value=[]), \
         patch("app.api.review._set_review_progress") as mock_progress:
        _run_review_pipeline(
            schedule_bytes=b"", narrative_bytes=b"", sp_bytes=None,
            keymap_bytes=None, estimate_bytes=None, selected_checks=None,
            project_id="p1", reseed=False, user_id="user-1",
        )

    messages = [c.kwargs.get("message") for c in mock_progress.call_args_list]
    assert messages == [
        "Loading saved project data…",
        "Preparing compliance checklist…",
        "Running compliance checks (0/57)…",
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_review_endpoint.py::test_run_review_pipeline_fresh_review_reports_unconditional_stage_messages backend/tests/test_review_endpoint.py::test_run_review_pipeline_fresh_review_reports_conditional_stage_messages backend/tests/test_review_rerun_endpoint.py::test_run_review_pipeline_rerun_reports_single_fast_path_message -v`
Expected: all three FAIL — `mock_progress.call_args_list` is empty or missing the new messages, since none of the new `_set_review_progress` calls exist yet.

- [ ] **Step 3: Add the stage messages to `_run_review_pipeline`**

In `backend/app/api/review.py`, apply these six changes inside `_run_review_pipeline` (712-969), in the order they appear in the file:

**3a.** Before `seed_schedule(...)`, change:

```python
        # ── Seed Neo4j (schedule + narrative + SP), fenced to this project_id ──
        seed_schedule(graph, activities, calendars, cpm, xcheck, project, project_id=project_id)
```

to:

```python
        # ── Seed Neo4j (schedule + narrative + SP), fenced to this project_id ──
        _set_review_progress(project_id, status="running", message="Seeding schedule graph…")
        seed_schedule(graph, activities, calendars, cpm, xcheck, project, project_id=project_id)
```

**3b.** Before narrative parsing/seeding, change:

```python
        narrative_pages = _bytes_to_pdf_pages(narrative_bytes)
        nar_chunks = chunk_narrative(narrative_pages)
```

to:

```python
        _set_review_progress(project_id, status="running", message="Seeding narrative graph…")
        narrative_pages = _bytes_to_pdf_pages(narrative_bytes)
        nar_chunks = chunk_narrative(narrative_pages)
```

**3c.** In the SP block, change:

```python
        sp_search_fn = None
        if sp_bytes:
            sp_chunks = _bytes_to_sp_chunks(sp_bytes)
```

to:

```python
        sp_search_fn = None
        if sp_bytes:
            _set_review_progress(project_id, status="running", message="Processing special provision…")
            sp_chunks = _bytes_to_sp_chunks(sp_bytes)
```

**3d.** In the keymap block, change:

```python
        keymap_extraction: Optional[KeyMapExtraction] = None
        if keymap_bytes:
            keymap_extraction = _extract_and_store_keymap(
```

to:

```python
        keymap_extraction: Optional[KeyMapExtraction] = None
        if keymap_bytes:
            _set_review_progress(project_id, status="running", message="Extracting key map…")
            keymap_extraction = _extract_and_store_keymap(
```

**3e.** In the estimate block, change:

```python
        estimate_extraction: Optional[EstimateExtraction] = None
        if estimate_bytes:
            estimate_extraction = _extract_and_store_estimate(
```

to:

```python
        estimate_extraction: Optional[EstimateExtraction] = None
        if estimate_bytes:
            _set_review_progress(project_id, status="running", message="Extracting engineer's estimate…")
            estimate_extraction = _extract_and_store_estimate(
```

**3f.** In the utility plan block, change:

```python
        utility_plan_search_fn = None
        if utility_plan_bytes_list:
            utility_plan_chunks = []
```

to:

```python
        utility_plan_search_fn = None
        if utility_plan_bytes_list:
            _set_review_progress(project_id, status="running", message="Processing utility plans…")
            utility_plan_chunks = []
```

**3g.** At the top of the `else:` (fast-path/rerun) branch, change:

```python
    else:
        # ── Fast path: project already seeded — reuse existing chunks ───────
        project_name, duration_days = _read_project_summary(graph, project_id)
```

to:

```python
    else:
        # ── Fast path: project already seeded — reuse existing chunks ───────
        _set_review_progress(project_id, status="running", message="Loading saved project data…")
        project_name, duration_days = _read_project_summary(graph, project_id)
```

**3h.** After the `if reseed: ... else: ...` block ends (both branches converge here), before the EDQ seeding call, change:

```python
    _seed_edq_items_if_needed(graph, llm, embeddings, estimate_bytes, project_id, user_id)
```

to:

```python
    _set_review_progress(project_id, status="running", message="Preparing compliance checklist…")
    _seed_edq_items_if_needed(graph, llm, embeddings, estimate_bytes, project_id, user_id)
```

**3i.** Immediately before the `evaluate_checks(...)` call, change:

```python
    try:
        check_results = evaluate_checks(
            selected_checks or BUILTIN_CHECKS, graph, llm,
            sp_search_fn=sp_search_fn, spec_search_fn=spec_search_fn, csm_search_fn=csm_search_fn,
            keymap_facts=keymap_facts, keymap_geo=keymap_geo,
            estimate_facts=estimate_facts, cost_gap=cost_gap,
            edq_coverage=edq_coverage,
            utility_plan_search_fn=utility_plan_search_fn,
            project_id=project_id, user_id=user_id,
        )
    except Exception as exc:
```

to:

```python
    total_checks = len(selected_checks or BUILTIN_CHECKS)
    _set_review_progress(
        project_id, status="running",
        message=f"Running compliance checks (0/{total_checks})…",
    )
    try:
        check_results = evaluate_checks(
            selected_checks or BUILTIN_CHECKS, graph, llm,
            sp_search_fn=sp_search_fn, spec_search_fn=spec_search_fn, csm_search_fn=csm_search_fn,
            keymap_facts=keymap_facts, keymap_geo=keymap_geo,
            estimate_facts=estimate_facts, cost_gap=cost_gap,
            edq_coverage=edq_coverage,
            utility_plan_search_fn=utility_plan_search_fn,
            project_id=project_id, user_id=user_id,
            on_progress=lambda done, total: _set_review_progress(
                project_id, status="running",
                message=f"Running compliance checks ({done}/{total})…",
            ),
        )
    except Exception as exc:
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_review_endpoint.py::test_run_review_pipeline_fresh_review_reports_unconditional_stage_messages backend/tests/test_review_endpoint.py::test_run_review_pipeline_fresh_review_reports_conditional_stage_messages backend/tests/test_review_rerun_endpoint.py::test_run_review_pipeline_rerun_reports_single_fast_path_message -v`
Expected: all three PASS

- [ ] **Step 5: Run the full backend suite to check for regressions**

Run: `.venv/Scripts/python.exe -m pytest backend/tests -q`
Expected: all tests pass, including every pre-existing test in `test_review_endpoint.py` and `test_review_rerun_endpoint.py` that mocks `_run_review_pipeline` wholesale (unaffected — they never call the real function body) and any that DO call the real function indirectly via `_run_review_pipeline_background`/`_run_review_rerun_background` with `_run_review_pipeline` mocked out (also unaffected, same reason).

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/review.py backend/tests/test_review_endpoint.py backend/tests/test_review_rerun_endpoint.py
git commit -m "feat: report pipeline stage progress through _run_review_pipeline"
```

---

### Task 3: Frontend — show live progress instead of the static loading text

**Files:**
- Modify: `frontend/src/components/review/DocumentReview.tsx:268-269` (state), `:401-402` (runReview start), `:479-495` (onmessage), `:960-962` (render)

**Interfaces:**
- Consumes: the `message` field already present on every SSE event (`{status, message, result?}`), unchanged shape from the backend — no new frontend types needed.

- [ ] **Step 1: Add the `loadingMessage` state**

In `frontend/src/components/review/DocumentReview.tsx`, change:

```typescript
  const [isLoading,     setIsLoading]     = useState(false)
  const [error,         setError]         = useState<string | null>(null)
```

to:

```typescript
  const [isLoading,     setIsLoading]     = useState(false)
  const [loadingMessage, setLoadingMessage] = useState('Starting review…')
  const [error,         setError]         = useState<string | null>(null)
```

- [ ] **Step 2: Reset the message when a review starts**

Change:

```typescript
    setIsLoading(true)
    setError(null)
```

to:

```typescript
    setIsLoading(true)
    setLoadingMessage('Starting review…')
    setError(null)
```

- [ ] **Step 3: Track the message on every non-terminal SSE event**

In `runReview()`'s `es.onmessage` handler, change:

```typescript
        onErrorCount = 0

        if (progress.status === 'error') {
          es.close()
          setError(progress.message || 'An unexpected error occurred.')
          setIsLoading(false)
          submittingRef.current = false
          return
        }
        if (progress.status !== 'ready' || !progress.result) return
```

to:

```typescript
        onErrorCount = 0

        if (progress.status === 'error') {
          es.close()
          setError(progress.message || 'An unexpected error occurred.')
          setIsLoading(false)
          submittingRef.current = false
          return
        }
        if (progress.message) {
          setLoadingMessage(progress.message)
        }
        if (progress.status !== 'ready' || !progress.result) return
```

- [ ] **Step 4: Render the live message instead of the static text**

Change:

```tsx
            <span className="text-sm font-medium text-[#1B3A6B]">
              Analyzing documents… this may take up to 1 min 30 seconds
            </span>
```

to:

```tsx
            <span className="text-sm font-medium text-[#1B3A6B]">
              {loadingMessage}
            </span>
```

- [ ] **Step 5: Type-check**

Run: `cd frontend && npx tsc --noEmit -p .`
Expected: clean, no output, exit code 0

- [ ] **Step 6: Manual verification**

No frontend unit-test harness exists for this component (confirmed in prior work on this file). Verify manually:
1. Start the dev server and open the review page.
2. Submit a review and confirm the loading indicator's text starts as "Starting review…", then changes as the backend reports each stage ("Seeding schedule graph…", etc.), then shows "Running compliance checks (X/57)…" with `X` increasing during check evaluation.
3. Confirm the spinner icon's position is unchanged (still to the left of the text).
4. Confirm submitting a review that errors still shows the error message (not the loading text) — the existing `'error'` branch is untouched.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/review/DocumentReview.tsx
git commit -m "feat: show live pipeline progress instead of a static loading message"
```
