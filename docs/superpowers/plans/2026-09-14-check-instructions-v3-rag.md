# Check Instructions v3 and SP Retrieval Parity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make compliance-check verdicts stop contradicting their own evidence, by first removing the retrieval nondeterminism that makes the same check answer differently on identical input, then rolling out the v3 instruction set whose decision rules are mechanically binding.

**Architecture:** The compliance engine builds one prompt per check as `evidence + "CHECK: " + name + instruction`, and for `sp`-sourced checks the *same instruction string* is also the vector query. So a check instruction is doing two jobs at once, and its opening words drive retrieval while its closing words drive the verdict. This plan fixes the retrieval layer first (two divergent SP search implementations, only one of which applies a similarity threshold), adds telemetry so retrieval outcomes stop being invisible, then rewrites instructions anchor-first with the decision rule last.

**Tech Stack:** Python 3.11, FastAPI, LangChain `with_structured_output`, OpenAI embeddings + chat (temperature 0), Supabase pgvector (`session_chunks`, `match_session_chunks` RPC), Neo4j, pytest.

**Spec:** `docs/superpowers/specs/2026-09-14-check-instructions-v3.md`. Every instruction body in Tasks 6 through 9 is copied verbatim from that file. Do not paraphrase.

**Worktree:** `.claude/worktrees/rag-check-instructions-v3` on branch `rag-check-instructions-v3`, based on `compliance-review-fixes` (commit `a62f594`). Baseline verified: 193 tests pass.

---

## Global Constraints

- **Do not change** any `check_key`, `category`, `name`, or the set of checks. 57 built-ins, and that count stays 57.
- **Ask the user before** adding a dependency, changing the output schema, or touching the database. Tasks 10 and 11 touch the database and are explicitly gated.
- **One concern per commit.** Do not reformat unrelated code.
- **Never read or print** `backend/local.env.local`, `backend/.env`, or any secret value.
- **There is no `Warning` status in this codebase.** Statuses are `Pass`, `Fail`, `Missing`. `Missing` renders as the amber MISSING pill. Every "-> Warning" in the spec means "set `insufficient_evidence` true", which `_derive_status` turns into `Missing`. An instruction that tells the model to emit a `Warning` status is a bug, because the model authors no status field.
- **Test command,** run from the worktree root:
  ```bash
  PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q
  ```
  The repo-root `.venv` is the one with pytest installed. `backend/.venv` does not have it.
- **`backend/.gitignore` contains a blanket `*`.** The Grep tool silently returns nothing for files under `backend/`. Use `rg --no-ignore` through Bash, or the Read tool.
- **Deterministic check types** (`geo`, `cost_gap`, `edq_coverage`, `date_rule`, `schedule_logic`) never read `instruction` at evaluation time. Their instruction text is UI copy only and cannot change behavior.

---

## Verified architecture facts

Confirmed against the code in this worktree on 2026-09-14. The spec asserted the first four; all four hold.

| Fact | Evidence |
|---|---|
| The instruction is the literal last line of the prompt | `backend/app/compliance/eval_engine.py:760` |
| For `sp` checks the instruction is the vector query | `backend/app/compliance/eval_engine.py:741` |
| Evidence block names are literal | `eval_engine.py:236` facts, `:315` milestones, `:340` activities |
| The activity roster carries no calendar column | `eval_engine.py:340` |
| Chat temperature is 0 | `backend/app/api/review.py:830` |
| 24 catalog lines carry `"sp"` in `source_files`; 4 checks raise `sp_top_k` to 12 | `backend/app/compliance/catalog.py:116,487,830,860` |

### The finding that answers the spec's Question 2

**There are two Special Provision search implementations, and they disagree.**

- `_build_sp_search_fn` at `backend/app/api/review.py:345` is the fresh-review path. It sorts every SP chunk by cosine similarity and slices the top `top_k`. **It applies no similarity threshold at all.** With a non-empty SP it always returns `top_k` chunks, however weak the match. Its `if not top` branch at line 366 is unreachable in practice.
- `_build_sp_search_fn_from_supabase` at `backend/app/api/review.py:382` is the rerun fast path. It delegates to `retrieve_sp_chunks`, which passes `match_threshold = 0.2` (`backend/app/retrieval_langchain/sp_retriever.py:23`). When nothing clears 0.2 it returns **zero rows**, and the check receives the literal string `"No matching Special Provision text found."` as its entire SP evidence.

The check still runs in that case. The missing-sources gate at `eval_engine.py:713` only fires when `sp_search_fn is None`, meaning no SP was uploaded at all. A retrieval that returns nothing is invisible to it, so the model answers from the narrative alone and reports `Source: narrative`.

That is the reported symptom exactly. `traffic_control_staging` showing `Source: narrative and schedule milestones/WBS` with no SP text, and `narrative_night_work` quoting the safe-time clause in report 22 but not in report 23, are not model nondeterminism. They are two different retrieval implementations selected by which code path ran. **No instruction rewrite can fix this, which is why Tasks 1 through 3 come before any instruction edit.**

The two paths also join passages identically (`"\n\n---\n\n"`), so formatting is not a divergence. Only the threshold is.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `backend/app/retrieval_langchain/sp_retriever.py` | SP vector search against Supabase | Modify: accept a caller-supplied threshold |
| `backend/app/api/review.py` | Builds both SP search closures | Modify: unify threshold semantics, emit telemetry |
| `backend/app/compliance/retrieval_log.py` | **New.** One dataclass plus a module-level sink recording what evidence each check received | Create |
| `backend/app/compliance/eval_engine.py` | Prompt assembly, verdict derivation | Modify: record telemetry, flag an SP miss, extend the system prompt |
| `backend/app/compliance/catalog.py` | The 57 check definitions | Modify: instruction bodies only |
| `backend/scripts/sp_retrieval_probe.py` | **New.** Offline probe reporting per-check SP retrieval quality | Create |
| `backend/scripts/refresh_check_instructions.py` | **New.** Push catalog instruction text onto forked user rows | Create |
| `backend/tests/test_retrieval_log.py` | **New.** Telemetry unit tests | Create |
| `backend/tests/test_sp_search_parity.py` | **New.** The two SP closures agree | Create |
| `backend/tests/test_eval_engine.py` | Existing engine tests | Modify: SP-miss and prompt tests |
| `backend/tests/test_catalog_instructions.py` | **New.** Structural invariants over all 57 instructions | Create |

Telemetry lives in its own module rather than inside `eval_engine.py` because `eval_engine.py` is already the largest file in the compliance package and the probe script in Task 3 needs to import the record type without importing the engine.

---

## Task 1: Give SP retrieval one threshold, set by the caller

**Files:**
- Modify: `backend/app/retrieval_langchain/sp_retriever.py:23-52`
- Test: `backend/tests/test_sp_search_parity.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `retrieve_sp_chunks(db, embed_fn, project_id, query, match_count=8, match_threshold=0.0)` — the new keyword argument, defaulting to `0.0` so every existing caller keeps working while no longer silently dropping rows.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_sp_search_parity.py`:

```python
"""backend/tests/test_sp_search_parity.py

The two Special Provision search closures must return the same number of
passages for the same query. They used to disagree: the fresh-review path
applied no similarity threshold, the rerun path applied 0.2, so the same
check saw 8 chunks on one run and none on the next.

No network, no real Supabase -- the db is a fake returning canned rows.

    python -m pytest backend/tests/test_sp_search_parity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.retrieval_langchain.sp_retriever import retrieve_sp_chunks  # noqa: E402


class _FakeRPC:
    def __init__(self, rows):
        self.rows = rows
        self.params = None

    def execute(self):
        return type("R", (), {"data": self.rows})()


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.last_params = None

    def rpc(self, name, params):
        self.last_params = params
        return _FakeRPC(self.rows)


def test_retrieve_sp_chunks_defaults_to_no_threshold():
    # A weak-but-best match must still come back. Dropping it is what made
    # the rerun path return nothing and the check answer from narrative only.
    db = _FakeDB(rows=[{"content": "105.07.02 ...", "similarity": 0.11}])
    rows = retrieve_sp_chunks(db, lambda q: [0.0] * 3, "proj-1", "night work")
    assert db.last_params["match_threshold"] == 0.0
    assert len(rows) == 1


def test_retrieve_sp_chunks_honours_an_explicit_threshold():
    db = _FakeDB(rows=[])
    retrieve_sp_chunks(db, lambda q: [0.0] * 3, "proj-1", "night work", match_threshold=0.35)
    assert db.last_params["match_threshold"] == 0.35


if __name__ == "__main__":
    test_retrieve_sp_chunks_defaults_to_no_threshold()
    test_retrieve_sp_chunks_honours_an_explicit_threshold()
    print("All tests passed!")
```

- [ ] **Step 2: Run it to make sure it fails**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_sp_search_parity.py -q
```

Expected: FAIL. `test_retrieve_sp_chunks_defaults_to_no_threshold` asserts `match_threshold == 0.0` but the module hardcodes `_MATCH_THRESHOLD = 0.2`, and `test_retrieve_sp_chunks_honours_an_explicit_threshold` fails with `TypeError: retrieve_sp_chunks() got an unexpected keyword argument 'match_threshold'`.

- [ ] **Step 3: Make the threshold a parameter**

In `backend/app/retrieval_langchain/sp_retriever.py`, change the signature and the RPC payload:

```python
# Retrieval used to drop every chunk below 0.2 cosine. For chat that is a
# reasonable floor. For compliance checks it is not: the query is the check
# INSTRUCTION, a 150-word rule, which embeds far from a 600-token SP clause
# even when that clause is the right one. Below the floor the check received
# no SP text at all and answered from the narrative, silently. Callers now
# choose; the default keeps the best-ranked passages rather than none.
_DEFAULT_MATCH_THRESHOLD = 0.0


def retrieve_sp_chunks(
    db: Any,
    embed_fn: Callable[[str], List[float]],
    project_id: str,
    query: str,
    match_count: int = 8,
    match_threshold: float = _DEFAULT_MATCH_THRESHOLD,
) -> List[Dict[str, Any]]:
```

and inside the RPC parameter dict replace `"match_threshold": _MATCH_THRESHOLD,` with `"match_threshold": match_threshold,`.

Leave the docstring's first paragraph intact and add one line to it:

```
    ``match_threshold`` defaults to 0.0 so a caller gets the best-ranked
    ``match_count`` passages rather than an empty list; pass a floor
    explicitly where a weak match is worse than none.
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_sp_search_parity.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Run the whole suite**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q
```

Expected: `195 passed`. If any existing test asserted the 0.2 floor, read it before changing it. Chat retrieval callers are allowed to keep a floor by passing one.

- [ ] **Step 6: Check whether chat should keep its floor**

```bash
rg --no-ignore -n "retrieve_sp_chunks" backend/app backend/scripts
```

For every caller outside `review.py`, decide explicitly. A chat caller answering a user question benefits from a floor, so pass `match_threshold=0.2` there to preserve today's behavior. Record the decision in a comment at each call site. Do not leave a caller's behavior changed by accident.

- [ ] **Step 7: Commit**

```bash
git add backend/app/retrieval_langchain/sp_retriever.py backend/tests/test_sp_search_parity.py
git commit -m "fix(retrieval): let the caller set the SP similarity floor"
```

---

## Task 2: Make an empty SP retrieval visible instead of silent

**Files:**
- Modify: `backend/app/compliance/eval_engine.py:740-743` (the `"sp" in sources` branch)
- Test: `backend/tests/test_eval_engine.py`

**Interfaces:**
- Consumes: `retrieve_sp_chunks`'s new default from Task 1.
- Produces: module constant `_SP_MISS_SENTINEL = "No matching Special Provision text found."` exported from `eval_engine`, used by Task 3's telemetry.

A retrieval that returns nothing must not produce a confident Pass off the narrative. With Task 1 this should become rare, but rare is not never, and the failure must be legible when it happens.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_eval_engine.py`:

```python
def test_evaluate_one_check_flags_an_empty_sp_retrieval_as_missing():
    """SP uploaded, but the vector search matched nothing. Answering from the
    narrative alone and calling it COMPLIANT is the bug; the check must report
    needs-review instead, naming what was not retrieved."""
    check = _make_check(check_key="multi_year_funding", source_files=["sp", "schedule"])

    def _empty_sp(query, top_k=8):
        return "No matching Special Provision text found.", {}

    llm = _FakeStructuredLLM([])   # must not be reached
    judge = _FakeStructuredLLM([])

    result, usage = _call_evaluate_one_check(check, llm, judge, sp_search_fn=_empty_sp)

    assert result.status == "Missing"
    assert "Special Provision" in result.evidence
    assert usage["llm_call_count"] == 0
```

If the existing `_call_evaluate_one_check` helper does not accept `sp_search_fn`, add the parameter and thread it through to `_evaluate_one_check`. Read the helper first; do not duplicate it.

- [ ] **Step 2: Run it to make sure it fails**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py::test_evaluate_one_check_flags_an_empty_sp_retrieval_as_missing -q
```

Expected: FAIL. The check currently calls the LLM with the sentinel string as its SP evidence, so `llm_call_count` is 1 and `_FakeStructuredLLM([])` raises `IndexError` or `StopIteration`.

- [ ] **Step 3: Short-circuit on the sentinel**

Near the other module constants in `backend/app/compliance/eval_engine.py`, add:

```python
# Both SP search closures in app.api.review return exactly this string when
# the vector search matched nothing. The missing-sources gate below cannot
# see that case -- it only knows whether an SP was UPLOADED -- so a check
# would otherwise answer from its other sources and report a confident Pass
# with no SP text behind it.
_SP_MISS_SENTINEL = "No matching Special Provision text found."
```

Replace the `"sp" in sources` branch at roughly line 740 with:

```python
    if "sp" in sources:
        sp_text, sp_candidates = sp_search_fn(check.instruction, top_k=check.sp_top_k)
        if sp_text.strip() == _SP_MISS_SENTINEL:
            return ReviewCheckResult(
                id=check.check_key, category=check.category, name=check.name,
                status="Missing",
                evidence=(
                    "The Special Provision was uploaded but no passage matched this "
                    "check's retrieval query, so the rule could not be evaluated "
                    "against Special Provision text."
                ),
                source="Special Provision (no matching passage retrieved)",
            ), usage_totals
        evidence_parts.append(sp_text)
        citation_lookup.update(sp_candidates)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py -q
```

Expected: all tests in the file pass, including the new one.

- [ ] **Step 5: Run the whole suite**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q
```

Expected: `196 passed`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/compliance/eval_engine.py backend/tests/test_eval_engine.py
git commit -m "fix(compliance): report Missing when SP retrieval returns no passage"
```

---

## Task 3: Record what evidence every check actually received

**Files:**
- Create: `backend/app/compliance/retrieval_log.py`
- Modify: `backend/app/compliance/eval_engine.py` (after evidence assembly, before `user_msg`)
- Test: `backend/tests/test_retrieval_log.py` (create)

**Interfaces:**
- Consumes: `_SP_MISS_SENTINEL` from Task 2.
- Produces:
  - `@dataclass CheckEvidenceRecord(check_key: str, sources: tuple[str, ...], blocks_present: tuple[str, ...], sp_passages: int, sp_chars: int, evidence_chars: int)`
  - `record(rec: CheckEvidenceRecord) -> None`
  - `drain() -> list[CheckEvidenceRecord]` — returns and clears
  - `as_rows() -> list[dict]` — JSON-ready

This answers the spec's Question 1 directly. `traffic_control_staging` and `narrative_work_hour_restrictions` stop being a guess.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_retrieval_log.py`:

```python
"""backend/tests/test_retrieval_log.py

Per-check evidence telemetry. Answers "did this check actually receive SP
text?" without re-running a full review and reading prose.

    python -m pytest backend/tests/test_retrieval_log.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.compliance.retrieval_log import (  # noqa: E402
    CheckEvidenceRecord, as_rows, drain, record,
)


def setup_function():
    drain()


def test_drain_returns_records_then_clears():
    record(CheckEvidenceRecord(
        check_key="traffic_control_staging", sources=("sp", "narrative", "schedule"),
        blocks_present=("narrative", "schedule"), sp_passages=0, sp_chars=0,
        evidence_chars=4200,
    ))
    rows = drain()
    assert len(rows) == 1
    assert rows[0].check_key == "traffic_control_staging"
    assert rows[0].sp_passages == 0
    assert drain() == []


def test_as_rows_is_json_ready_and_marks_sp_starved_checks():
    record(CheckEvidenceRecord(
        check_key="narrative_work_hour_restrictions", sources=("narrative", "sp"),
        blocks_present=("narrative",), sp_passages=0, sp_chars=0, evidence_chars=900,
    ))
    record(CheckEvidenceRecord(
        check_key="row_availability", sources=("sp", "narrative", "schedule"),
        blocks_present=("schedule", "narrative", "sp"), sp_passages=8, sp_chars=5100,
        evidence_chars=9000,
    ))
    rows = as_rows()
    starved = [r for r in rows if "sp" in r["sources"] and r["sp_passages"] == 0]
    assert [r["check_key"] for r in starved] == ["narrative_work_hour_restrictions"]
    assert all(isinstance(r["sp_chars"], int) for r in rows)


if __name__ == "__main__":
    setup_function(); test_drain_returns_records_then_clears()
    setup_function(); test_as_rows_is_json_ready_and_marks_sp_starved_checks()
    print("All tests passed!")
```

- [ ] **Step 2: Run it to make sure it fails**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_retrieval_log.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.compliance.retrieval_log'`.

- [ ] **Step 3: Write the module**

Create `backend/app/compliance/retrieval_log.py`:

```python
"""Per-check evidence telemetry.

Answers one question that prose output cannot: for THIS check, on THIS run,
which evidence blocks were actually assembled, and how many Special Provision
passages came back? A check with "sp" in its sources and sp_passages == 0
answered without the document its rule names.

Process-local and not thread-safe by design: a review evaluates checks
concurrently, so the list is appended under a lock and read once at the end.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class CheckEvidenceRecord:
    check_key: str
    sources: Tuple[str, ...]
    blocks_present: Tuple[str, ...]
    sp_passages: int
    sp_chars: int
    evidence_chars: int


_lock = threading.Lock()
_records: List[CheckEvidenceRecord] = []


def record(rec: CheckEvidenceRecord) -> None:
    with _lock:
        _records.append(rec)


def drain() -> List[CheckEvidenceRecord]:
    """Return everything recorded so far and clear the buffer."""
    with _lock:
        out = list(_records)
        _records.clear()
    return out


def as_rows() -> List[Dict]:
    """JSON-ready snapshot. Drains, so call it once per review."""
    return [asdict(r) for r in drain()]
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_retrieval_log.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Emit a record from the engine**

In `backend/app/compliance/eval_engine.py`, import at the top:

```python
from app.compliance.retrieval_log import CheckEvidenceRecord, record as record_evidence
```

Track SP passage count where the SP block is appended. The passage separator is `"\n\n---\n\n"`, so the count is `sp_text.count("\n\n---\n\n") + 1` when text was returned. Introduce two locals before the source branches:

```python
    sp_passages = 0
    sp_chars = 0
    blocks_present: List[str] = []
```

Append the block name inside each branch that adds to `evidence_parts` (`"schedule"`, `"narrative"`, `"sp"`, `"keymap"`, `"estimate"`, `"spec"`, `"csm"`, `"utility_plan"`). In the SP branch, after the sentinel short-circuit:

```python
        sp_passages = sp_text.count("\n\n---\n\n") + 1
        sp_chars = len(sp_text)
```

Then immediately before `user_msg = ...` at roughly line 760:

```python
    record_evidence(CheckEvidenceRecord(
        check_key=check.check_key, sources=tuple(sources),
        blocks_present=tuple(blocks_present), sp_passages=sp_passages,
        sp_chars=sp_chars, evidence_chars=len(evidence),
    ))
```

- [ ] **Step 6: Run the whole suite**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q
```

Expected: `198 passed`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/compliance/retrieval_log.py backend/app/compliance/eval_engine.py backend/tests/test_retrieval_log.py
git commit -m "feat(compliance): record which evidence blocks each check received"
```

---

## Task 4: Offline probe for SP retrieval quality per check

**Files:**
- Create: `backend/scripts/sp_retrieval_probe.py`

**Interfaces:**
- Consumes: `BUILTIN_CHECKS` from the catalog, `retrieve_sp_chunks` with its Task 1 signature.
- Produces: a JSON report at `backend/data/eval/sp_probe_<label>.json` with one row per `sp` check: `check_key`, `rows_returned`, `top_similarity`, `mean_similarity`, `anchor_hit` (whether the top passage contains the section number the instruction opens with).

This is the measurement instrument. It runs without an LLM, so it is cheap enough to run before and after every instruction change.

- [ ] **Step 1: Write the script**

Create `backend/scripts/sp_retrieval_probe.py`:

```python
"""sp_retrieval_probe.py -- how well does each sp check retrieve?

For every BUILTIN_CHECK with "sp" in source_files, embeds the check's
instruction exactly as app.compliance.eval_engine does, runs the same vector
search, and reports how many passages came back and how similar the best one
was. No LLM call, so this is cheap to run repeatedly.

Usage
-----
    python scripts/sp_retrieval_probe.py --project-id <review-project-id> \
        --label before
    python scripts/sp_retrieval_probe.py --project-id <id> --label after

Compare two runs:
    python scripts/sp_retrieval_probe.py --compare before after
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

from dotenv import load_dotenv

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
load_dotenv(_BACKEND / ".env")

from app.compliance.catalog import BUILTIN_CHECKS  # noqa: E402
from app.database import get_db  # noqa: E402
from app.retrieval_langchain.sp_retriever import retrieve_sp_chunks  # noqa: E402

_OUT_DIR = _BACKEND / "data" / "eval"
# "105.07.02", "SECTION 703", "TABLE 105.05-1" -- the anchor the instruction
# is supposed to open with, per the v3 template's rule 2.
_ANCHOR_RE = re.compile(r"\b(?:\d{3}\.\d{2}(?:\.\d{2})?|SECTION\s+\d{3}|TABLE\s+[\d.\-]+)")


def _first_anchor(instruction: str) -> str | None:
    head = instruction.split("\n\n", 1)[0]
    m = _ANCHOR_RE.search(head)
    return m.group(0) if m else None


def probe(project_id: str, label: str) -> None:
    from langchain_openai import OpenAIEmbeddings
    import app.config as config

    db = get_db()
    embeddings = OpenAIEmbeddings(model=config.EMBEDDING_MODEL, api_key=config.OPENAI_API_KEY)

    rows_out = []
    sp_checks = [c for c in BUILTIN_CHECKS if "sp" in c.source_files]
    print(f"-- probing {len(sp_checks)} sp checks against project {project_id}")

    for c in sp_checks:
        rows = retrieve_sp_chunks(
            db, embeddings.embed_query, project_id, c.instruction, match_count=c.sp_top_k,
        )
        sims = [r.get("similarity") or 0.0 for r in rows]
        anchor = _first_anchor(c.instruction)
        top_text = rows[0]["content"] if rows else ""
        rows_out.append({
            "check_key": c.check_key,
            "sp_top_k": c.sp_top_k,
            "rows_returned": len(rows),
            "top_similarity": round(max(sims), 4) if sims else 0.0,
            "mean_similarity": round(statistics.fmean(sims), 4) if sims else 0.0,
            "first_anchor": anchor,
            "anchor_in_top_passage": bool(anchor and anchor in top_text),
        })
        flag = "" if rows else "   <-- NO ROWS"
        print(f"   {c.check_key:<38} rows={len(rows):>2} top={rows_out[-1]['top_similarity']:.3f}{flag}")

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = _OUT_DIR / f"sp_probe_{label}.json"
    out.write_text(json.dumps(rows_out, indent=2), encoding="utf-8")
    print(f"\nOK wrote {out}")
    starved = [r["check_key"] for r in rows_out if r["rows_returned"] == 0]
    missed = [r["check_key"] for r in rows_out if r["first_anchor"] and not r["anchor_in_top_passage"]]
    print(f"   checks with no SP rows: {len(starved)} {starved}")
    print(f"   checks whose anchor is absent from the top passage: {len(missed)}")


def compare(before: str, after: str) -> None:
    b = {r["check_key"]: r for r in json.loads((_OUT_DIR / f"sp_probe_{before}.json").read_text())}
    a = {r["check_key"]: r for r in json.loads((_OUT_DIR / f"sp_probe_{after}.json").read_text())}
    print(f"{'check_key':<38} {'rows':>9}  {'top similarity':>16}")
    for key in sorted(set(b) | set(a)):
        rb, ra = b.get(key, {}), a.get(key, {})
        print(f"{key:<38} {rb.get('rows_returned', '-'):>4} -> {ra.get('rows_returned', '-'):<3} "
              f"{rb.get('top_similarity', 0):>7.3f} -> {ra.get('top_similarity', 0):<7.3f}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--project-id")
    p.add_argument("--label", default="run")
    p.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    args = p.parse_args()
    if args.compare:
        compare(*args.compare)
    elif args.project_id:
        probe(args.project_id, args.label)
    else:
        p.error("pass --project-id, or --compare BEFORE AFTER")
```

- [ ] **Step 2: Confirm the embedding config name**

```bash
rg --no-ignore -n "EMBEDDING_MODEL|CHAT_MODEL" backend/app/config.py
```

If the constant is named differently, fix the import in the script. Do not guess.

- [ ] **Step 3: Find a project id with SP chunks**

```bash
rg --no-ignore -n "special_provision" backend/app/api/review.py | head -5
```

Then ask the user for the `review_projects` id of a review that had an SP uploaded, or use the Route 49 fixture project if one is already seeded. **Do not query the database for user data without asking.**

- [ ] **Step 4: Run the probe and read it**

```bash
cd backend && PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe scripts/sp_retrieval_probe.py --project-id <id> --label before
```

Expected: one line per `sp` check. **This is the deliverable that answers the spec's Question 1.** Record which checks show `rows=0`, and specifically what `traffic_control_staging` and `narrative_work_hour_restrictions` show.

- [ ] **Step 5: Commit**

```bash
git add backend/scripts/sp_retrieval_probe.py
git commit -m "feat(eval): offline probe for per-check SP retrieval quality"
```

---

## Task 5: Add DECISION DISCIPLINE to the system prompt

**Files:**
- Modify: `backend/app/compliance/eval_engine.py:92-150` (`_STATIC_SYSTEM_PROMPT`)
- Test: `backend/tests/test_eval_engine.py`

**Interfaces:**
- Consumes: the existing `EvaluationSchema` three-field contract.
- Produces: no new symbols. Prompt text only.

The spec's block names a `Warning` verdict. This codebase has none, and the model authors no status at all. Adapt, do not paste.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_eval_engine.py`:

```python
def test_system_prompt_binds_the_verdict_and_forbids_a_contradicting_however():
    """The v3 failure mode was evidence text that contradicted the verdict --
    'outside the blackout windows' next to a Fail. The prompt must name that
    pattern, and must not invent a Warning status the schema cannot express."""
    from app.compliance.eval_engine import _STATIC_SYSTEM_PROMPT as p

    assert "DECISION DISCIPLINE" in p
    assert "may not contradict" in p
    assert "however" in p
    # The model has no status field; anything telling it to emit one is a bug.
    assert "-> Warning" not in p
    assert "insufficient_evidence" in p
```

- [ ] **Step 2: Run it to make sure it fails**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py::test_system_prompt_binds_the_verdict_and_forbids_a_contradicting_however -q
```

Expected: FAIL on `assert "DECISION DISCIPLINE" in p`.

- [ ] **Step 3: Append the adapted block**

At the end of `_STATIC_SYSTEM_PROMPT` in `backend/app/compliance/eval_engine.py`, before the closing `"""`, append:

```
DECISION DISCIPLINE

Fill in breaching_items BEFORE you write the evidence text, then let the \
verdict follow from it mechanically:
  breaching_items empty, insufficient_evidence false  -> the check passes
  breaching_items non-empty                           -> the check fails
  the material the rule needs is absent, or you cannot resolve a \
contradiction in what you were given -> insufficient_evidence true

Your evidence text may not contradict your own lists. If your evidence says \
the items fall OUTSIDE a restricted window, breaching_items is empty. If it \
names an item INSIDE the window, that item belongs in breaching_items. Do \
not write a sentence beginning "however" that reverses a conclusion you \
have already supported, and do not describe an item as breaching while \
leaving it out of breaching_items.

Quote only text that appears in the evidence blocks you were given. The \
blocks are named literally: PRECOMPUTED COMPLIANCE FACTS, MILESTONES, ALL \
SCHEDULE ACTIVITIES, and the retrieved Special Provision, narrative and \
specification passages. Attribute every quotation to the block it came from.
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q
```

Expected: `199 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/compliance/eval_engine.py backend/tests/test_eval_engine.py
git commit -m "feat(compliance): bind the verdict to breaching_items in the system prompt"
```

---

## Task 6: Structural invariants for every instruction

**Files:**
- Create: `backend/tests/test_catalog_instructions.py`

**Interfaces:**
- Consumes: `BUILTIN_CHECKS`.
- Produces: the guard rail Tasks 7 to 9 are checked against.

Write this **before** editing any instruction. It is what makes the rewrite safe to do in bulk.

- [ ] **Step 1: Write the test**

Create `backend/tests/test_catalog_instructions.py`:

```python
"""backend/tests/test_catalog_instructions.py

Structural invariants over all 57 built-in check instructions. These encode
the v3 template's architecture rules (see
docs/superpowers/specs/2026-09-14-check-instructions-v3.md) so a future edit
cannot quietly break them.

    python -m pytest backend/tests/test_catalog_instructions.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.compliance.catalog import BUILTIN_CHECKS  # noqa: E402

_DETERMINISTIC = {"geo", "cost_gap", "edq_coverage", "date_rule", "schedule_logic"}
_LLM_CHECKS = [c for c in BUILTIN_CHECKS if c.check_type == "llm"]
_SP_CHECKS = [c for c in _LLM_CHECKS if "sp" in c.source_files]
_ANCHOR_RE = re.compile(r"\b(?:\d{3}\.\d{2}(?:\.\d{2})?|SECTION\s+\d{3}|TABLE\s+[\d.\-]+)")


def test_the_catalog_still_holds_57_checks():
    # The rewrite changes instruction text only -- never the set of checks.
    assert len(BUILTIN_CHECKS) == 57
    assert len({c.check_key for c in BUILTIN_CHECKS}) == 57


def test_no_instruction_asks_for_a_status_the_schema_cannot_express():
    # The model authors no status field. "-> Warning" told it to emit one,
    # which is how absence-shaped breaches became green Passes.
    for c in BUILTIN_CHECKS:
        assert "-> Warning" not in c.instruction, c.check_key
        assert "-> WARNING" not in c.instruction, c.check_key


def test_every_llm_instruction_ends_with_its_verdict_rule():
    # The instruction is the literal last line of the prompt, so whatever
    # ends it is the last thing the model reads before answering.
    for c in _LLM_CHECKS:
        tail = c.instruction.strip().rsplit("\n\n", 1)[-1]
        assert "breaching_items" in tail, f"{c.check_key}: verdict rule is not last"


def test_every_sp_instruction_opens_with_a_retrieval_anchor():
    # For sp checks the instruction IS the vector query; a section number in
    # a trailing "Look in:" line embeds as a rule, not as a query.
    for c in _SP_CHECKS:
        head = c.instruction.split("\n\n", 1)[0]
        assert _ANCHOR_RE.search(head), f"{c.check_key}: no anchor in the first paragraph"


def test_no_instruction_asks_for_calendar_data_that_is_not_in_the_evidence():
    # The activity roster is id|name|phase|start|finish|duration_days|float|
    # critical. There is no calendar column, so a rule that turns on calendar
    # assignment can only be answered by guessing.
    banned = ("calendar assignment", "working days per week", "holiday exception")
    for c in _LLM_CHECKS:
        low = c.instruction.lower()
        for phrase in banned:
            if phrase in low:
                assert "not in your evidence" in low, f"{c.check_key}: asks for {phrase}"


def test_deterministic_instructions_say_so():
    for c in BUILTIN_CHECKS:
        if c.check_type in _DETERMINISTIC:
            assert "Deterministic" in c.instruction, c.check_key


if __name__ == "__main__":
    test_the_catalog_still_holds_57_checks()
    test_no_instruction_asks_for_a_status_the_schema_cannot_express()
    test_every_llm_instruction_ends_with_its_verdict_rule()
    test_every_sp_instruction_opens_with_a_retrieval_anchor()
    test_no_instruction_asks_for_calendar_data_that_is_not_in_the_evidence()
    test_deterministic_instructions_say_so()
    print("All tests passed!")
```

- [ ] **Step 2: Run it and read every failure**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_catalog_instructions.py -q
```

Expected: several FAIL. That failure list is the exact worklist for Tasks 7 to 9. Save it:

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_catalog_instructions.py -q > ../../../instruction-worklist.txt 2>&1
```

- [ ] **Step 3: Commit the test alone, failing**

Commit it now so the worklist is reviewable before any instruction changes.

```bash
git add backend/tests/test_catalog_instructions.py
git commit -m "test(catalog): encode the v3 instruction template as invariants"
```

---

## Task 7: Rewrite the SP-sourced instructions anchor-first

**Files:**
- Modify: `backend/app/compliance/catalog.py` — instruction bodies only
- Test: `backend/tests/test_catalog_instructions.py`

**Interfaces:**
- Consumes: the invariants from Task 6.
- Produces: no new symbols.

Copy each instruction body **verbatim** from the spec at `docs/superpowers/specs/2026-09-14-check-instructions-v3.md`, with one mechanical substitution defined below.

**The substitution.** Wherever the spec's last line reads `SP not retrieved -> Warning` or any `-> Warning` variant, write instead:

```
Empty -> Pass.  Non-empty -> Fail.  Special Provision section not retrieved
-> set insufficient_evidence true and say which section is missing.
```

Keep the Pass and Fail clauses exactly as the spec writes them. Only the Warning clause changes.

Checks in this task, all 24 with `"sp"` in `source_files`: `utility_alignment`, `row_availability`, `environmental_permit`, `gas_interruption`, `water_interruption`, `electric_interruption`, `utility_work_hours`, `railroad_restrictions`, `working_drawing_review_time`, `steel_pole_lead_time`, `aluminum_pole_lead_time`, `controller_lead_time`, `its_burn_in`, `narrative_row_requirements`, `narrative_work_hour_restrictions`, `narrative_night_work`, `traffic_control_staging`, `summer_shutdown`, `required_activities_present`, `multi_year_funding`, `nearby_projects`, plus any others the following command reports.

- [ ] **Step 1: List the exact set**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0, 'backend')
from app.compliance.catalog import BUILTIN_CHECKS
sp = [c for c in BUILTIN_CHECKS if 'sp' in c.source_files and c.check_type == 'llm']
print(len(sp)); [print(' ', c.check_key, c.sp_top_k) for c in sp]
"
```

Expected: a count and one line per check. Work this list, not the prose list above, if the two disagree.

- [ ] **Step 2: Apply the instructions, one check at a time**

For each check, find its entry in `backend/app/compliance/catalog.py` and replace the instruction string with the spec's block. Use the Edit tool with the full old string so a mismatch fails loudly rather than editing the wrong entry.

Do not touch `check_key`, `category`, `name`, `check_type`, `source_files`, or `sp_top_k` in this task.

- [ ] **Step 3: Run the invariants**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_catalog_instructions.py -q
```

Expected: `test_every_sp_instruction_opens_with_a_retrieval_anchor` and `test_no_instruction_asks_for_a_status_the_schema_cannot_express` now pass. `test_every_llm_instruction_ends_with_its_verdict_rule` may still fail for the non-SP checks Tasks 8 and 9 cover.

- [ ] **Step 4: Re-probe retrieval**

```bash
cd backend && PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe scripts/sp_retrieval_probe.py --project-id <id> --label after-anchors
cd backend && PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe scripts/sp_retrieval_probe.py --compare before after-anchors
```

Expected: `top_similarity` rises for most checks and `anchor_in_top_passage` flips to true for several. **If it does not move, the anchor-first hypothesis is wrong and Tasks 8 and 9 should not be assumed to help either.** Report that rather than continuing on faith.

- [ ] **Step 5: Run the whole suite and commit**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q
git add backend/app/compliance/catalog.py
git commit -m "feat(catalog): open every sp check instruction with its retrieval anchor"
```

---

## Task 8: Rewrite the schedule-only instructions as mechanical predicates

**Files:**
- Modify: `backend/app/compliance/catalog.py`
- Test: `backend/tests/test_catalog_instructions.py`

**Interfaces:**
- Consumes: Task 6's invariants.
- Produces: no new symbols.

These are the checks that oscillated. The spec writes their rules as arithmetic predicates specifically so the model has nothing left to weigh.

Checks: `landscape_season`, `temp_50_window`, `temp_60_window`, `no_concrete_winter`, `cold_weather_concreting`, `concrete_cure_time`, `no_paving_winter`, `cpm_consistency`.

- [ ] **Step 1: Apply the instruction bodies verbatim**

Copy each from the spec. `no_concrete_winter` is the important one: its Step 2 intersection test must land character for character, including the final paragraph that forbids the three excuses.

- [ ] **Step 2: Run the invariants**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_catalog_instructions.py -q
```

Expected: `test_no_instruction_asks_for_calendar_data_that_is_not_in_the_evidence` passes, because `no_concrete_winter` now says "Calendars are not in your evidence".

- [ ] **Step 3: Run the whole suite and commit**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q
git add backend/app/compliance/catalog.py
git commit -m "feat(catalog): state the winter and weather rules as arithmetic predicates"
```

---

## Task 9: Rewrite the narrative and UI-copy instructions

**Files:**
- Modify: `backend/app/compliance/catalog.py`
- Test: `backend/tests/test_catalog_instructions.py`

**Interfaces:**
- Consumes: Task 6's invariants.
- Produces: no new symbols.

Two groups.

**Narrative presence checks** (`narrative` sourced, no `sp`): `narrative_production_rates`, `narrative_workforce`, `narrative_winter_work`, `narrative_permit_requirements`, `narrative_utility_requirements`, `narrative_community_commitments`, `narrative_material_lead_time`, `narrative_detours`, `narrative_critical_milestones`, `narrative_schedule_problems`, `narrative_acceleration`, `narrative_winter_extension_reason`, `narrative_emergency_routes`.

**Deterministic UI copy** (`check_type` in the deterministic set, text never reaches an LLM): the six administrative date checks, the four completion-milestone checks, the four schedule-logic checks, and `edq_items`.

- [ ] **Step 1: Apply both groups verbatim from the spec**

- [ ] **Step 2: Run the invariants, which must now be fully green**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_catalog_instructions.py -q
```

Expected: `6 passed`. If `test_every_llm_instruction_ends_with_its_verdict_rule` still fails, the named check's instruction does not end with its `breaching_items` paragraph. Fix the instruction, not the test.

- [ ] **Step 3: Run the whole suite and commit**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q
git add backend/app/compliance/catalog.py
git commit -m "feat(catalog): narrative presence rules and deterministic UI copy"
```

---

## Task 10: Roll the instructions out to rows that already exist

**Files:**
- Create: `backend/scripts/refresh_check_instructions.py`

**Interfaces:**
- Consumes: `BUILTIN_CHECKS`.
- Produces: a script with `--dry-run` (default) and `--apply`.

**This task touches the database. Get the user's explicit approval before running it with `--apply`.**

Editing `catalog.py` alone changes nothing for an existing user. `_parse_checks` at `backend/app/api/review.py:707` reads `instruction` from the request payload, which the frontend fills from the user's own `compliance_checks` rows. `seed_compliance_checks.py` refreshes only `user_id IS NULL` rows. So a user who ever edited their checklist keeps the old instruction text forever.

This is the same class of bug as the `check_type` finding already fixed on this branch, and it is why `sp_top_k` and `check_type` are now taken from the catalog unconditionally. `instruction` cannot be handled the same way, because a user is allowed to edit a built-in check's rule, and overwriting that silently would destroy their work.

- [ ] **Step 1: Write the script**

Create `backend/scripts/refresh_check_instructions.py`:

```python
"""refresh_check_instructions.py -- push catalog instruction text onto rows
that were forked before the rewrite.

catalog.py is the source of truth for built-in rules, but a user who edited
their checklist holds their own copy of every row, instruction included, and
seed_compliance_checks.py only refreshes the shared user_id IS NULL set. So a
catalog rewrite reaches nobody who ever clicked a checkbox.

A user may legitimately have edited a built-in rule. This script therefore
reports three buckets and only rewrites the first:
  UNCHANGED  the row still matches some previous catalog text -> safe to update
  EDITED     the row matches no known catalog text -> left alone, listed
  CURRENT    already matches the new catalog text -> nothing to do

Usage
-----
    python scripts/refresh_check_instructions.py             # dry run
    python scripts/refresh_check_instructions.py --apply
    python scripts/refresh_check_instructions.py --apply --user <uuid>
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
load_dotenv(_BACKEND / ".env")

from app.compliance.catalog import BUILTIN_CHECKS  # noqa: E402
from app.database import get_db  # noqa: E402

TABLE = "compliance_checks"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="write; default is a dry run")
    p.add_argument("--user", help="limit to one user_id")
    p.add_argument("--baseline", type=Path,
                   help="file of previous catalog instructions, one JSON object "
                        "{check_key: instruction}; rows matching it count as UNCHANGED")
    args = p.parse_args()

    catalog = {c.check_key: c.instruction for c in BUILTIN_CHECKS}
    baseline = {}
    if args.baseline:
        import json
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))

    db = get_db()
    q = db.table(TABLE).select("id,user_id,check_key,instruction").not_.is_("user_id", "null")
    if args.user:
        q = q.eq("user_id", args.user)
    rows = q.execute().data

    buckets = defaultdict(list)
    for r in rows:
        want = catalog.get(r["check_key"])
        if want is None:
            buckets["CUSTOM"].append(r)          # user-authored check, never touch
        elif r["instruction"] == want:
            buckets["CURRENT"].append(r)
        elif baseline.get(r["check_key"]) == r["instruction"]:
            buckets["UNCHANGED"].append(r)
        else:
            buckets["EDITED"].append(r)

    for name in ("CURRENT", "UNCHANGED", "EDITED", "CUSTOM"):
        print(f"{name:<10} {len(buckets[name])}")
    if buckets["EDITED"]:
        print("\nEDITED rows are left alone. Review them by hand:")
        for r in buckets["EDITED"][:20]:
            print(f"   {str(r['user_id'])[:8]}...  {r['check_key']}")

    if not args.apply:
        print("\ndry run -- re-run with --apply to update the UNCHANGED rows")
        return
    if not buckets["UNCHANGED"]:
        print("\nnothing to update")
        return

    for r in buckets["UNCHANGED"]:
        db.table(TABLE).update({"instruction": catalog[r["check_key"]]}).eq("id", r["id"]).execute()
    print(f"\nOK updated {len(buckets['UNCHANGED'])} rows")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Capture the pre-rewrite baseline**

The `--baseline` file is what separates "never edited" from "user edited". Produce it from the commit before Task 7:

```bash
git show HEAD~3:backend/app/compliance/catalog.py > /tmp/catalog_before.py
```

Then write a throwaway script that imports that module and dumps `{check_key: instruction}` to JSON. Without this file every row lands in EDITED and nothing updates, which is the safe failure.

- [ ] **Step 3: Dry run**

```bash
cd backend && PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe scripts/refresh_check_instructions.py --baseline /tmp/catalog_before.json
```

Expected: four counts. Show them to the user.

- [ ] **Step 4: Re-seed the shared built-ins, then apply**

**Ask the user before either command.**

```bash
cd backend && PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe scripts/seed_compliance_checks.py
cd backend && PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe scripts/refresh_check_instructions.py --baseline /tmp/catalog_before.json --apply
```

- [ ] **Step 5: Commit the script**

```bash
git add backend/scripts/refresh_check_instructions.py
git commit -m "feat(scripts): refresh forked check instructions from the catalog"
```

---

## Task 11: Measure the result against the four reported failures

**Files:**
- No source changes. Produces `docs/superpowers/plans/2026-09-14-v3-results.md`.

**Interfaces:**
- Consumes: the probe from Task 4, the telemetry from Task 3.
- Produces: the report that decides whether this branch merges.

- [ ] **Step 1: Run a full review on the same input as report 23**

Use the same uploads. **Ask the user to run it from the UI, or for the project id to re-run**, since this spends API credit.

- [ ] **Step 2: Write the four named checks into a comparison table**

| check | report 23 | this run | agrees with its own evidence? |
|---|---|---|---|
| `environmental_permit` | Fail, evidence said outside the windows | | |
| `utility_alignment` | Warning, evidence said every item matched | | |
| `no_concrete_winter` | Pass, evidence listed D1120 in January | | |
| `working_drawing_review_time` | Fail on PS340 and PS350, correct | | |

- [ ] **Step 3: Run `no_concrete_winter` three times and compare**

The oscillation is the acceptance test. Three identical verdicts on identical input is the bar. **If it still flips with the arithmetic predicate in place, the cause is upstream of the prompt** — check whether the Anthropic fallback answered, by looking at which model served the call, and check the Task 3 telemetry for a differing `evidence_chars` between runs.

- [ ] **Step 4: Attach the probe comparison**

```bash
cd backend && PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe scripts/sp_retrieval_probe.py --compare before after-anchors
```

- [ ] **Step 5: State the verdict plainly**

Write whether the retrieval parity fix, the anchor-first rewrite, and the binding decision rule each moved their own metric, and name anything that did not. A change that did not move its metric should be called out, not folded into an aggregate improvement.

- [ ] **Step 6: Commit the report**

```bash
git add docs/superpowers/plans/2026-09-14-v3-results.md
git commit -m "docs: v3 instruction and retrieval results"
```

---

## Self-review notes

**Spec coverage.** Every instruction block in the spec maps to Task 7, 8, or 9. The shared `_STATIC_SYSTEM_PROMPT` addition is Task 5. The spec's Question 1 is answered by Tasks 3 and 4. Its Question 2 is answered by Task 1, which found the cause rather than merely measuring it.

**Two things the spec did not anticipate, both now tasks.**

1. The spec attributes the retrieval flapping to possible temperature or `sp_top_k` divergence. The real cause is two SP search implementations with different threshold semantics (Task 1). Chat temperature is already 0.
2. The spec assumes editing `catalog.py` rolls the instructions out. It does not, for any user who ever edited their checklist (Task 10).

**Ordering matters.** Tasks 1 through 4 must land before any instruction edit. Rewriting an instruction while retrieval can silently return nothing means measuring the rewrite against a moving baseline, and the spec's own Question 2 says as much.

**Type consistency.** `CheckEvidenceRecord`, `record`, `drain`, `as_rows` are defined in Task 3 and consumed in Tasks 4 and 11. `_SP_MISS_SENTINEL` is defined in Task 2 and referenced in Task 3. `retrieve_sp_chunks`'s `match_threshold` keyword is added in Task 1 and used in Task 4.
