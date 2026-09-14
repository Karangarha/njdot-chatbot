# Hybrid Check Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a compliance check that names a section or table actually receive it, on every run, by giving Special Provision chunks a section identity at ingestion and then retrieving through exact metadata lookup plus BM25 rank fusion instead of dense similarity alone.

**Architecture:** Three layers. Ingestion splits Special Provision text on detected section boundaries and records section identity and table captions. At query time an exact metadata lookup pins the named section or table into the evidence with no ranking involved, then BM25 and dense search are fused by Reciprocal Rank Fusion to fill the remaining budget. A new compliance retrieval module owns the check-specific composition; the shared ranker gives up a table-agnostic fusion helper.

**Tech Stack:** Python 3.11, Postgres with pgvector and full-text search through a local Supabase container, LangChain, OpenAI embeddings, pytest.

**Design:** `docs/superpowers/specs/2026-09-14-hybrid-check-retrieval-design.md`. Read it before starting.

**Worktree:** `.claude/worktrees/rag-check-instructions-v3` on branch `rag-check-instructions-v3`. Baseline: 193 tests pass.

---

## Global Constraints

- **Never read or print values from** `backend/local.env.local` or `backend/.env`. Variable names are fine; values are not. Load them with `dotenv` and pass them around, never echo them.
- **All database work targets the user's local Supabase container.** No hosted project is read or written by this plan.
- **Do not change** any `check_key`, `category`, `name`, or the set of 57 checks. Do not change the output schema.
- **One concern per commit.** Do not reformat unrelated code.
- **The chat path must not regress.** It keeps its own similarity floor and its existing ranker behaviour. Any change to `hybrid_ranker.py` is a pure extraction with identical behaviour.
- **`backend/.gitignore` is a blanket `*`.** The Grep tool returns nothing for files under `backend/`. Use `rg --no-ignore` through Bash, or the Read tool.
- **Test command,** from the worktree root:
  ```bash
  PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q
  ```
- **Integration tests must be skippable.** Mark every test that needs the local database with `@pytest.mark.integration` and skip when the environment is absent, so the default suite stays hermetic.

---

## Verified facts

Confirmed in this worktree on 2026-09-14. Four go beyond the design document.

| Fact | Where |
|---|---|
| SP chunks carry only `doc_type`, `page_pdf`, `chunk_index` | `session_chunker.py:415-430` |
| Chunk window is 600 tokens with 100 overlap | `session_chunker.py:40-41` |
| `section_detector.detect()` returns `{level, section_id, title}` or `None` | `section_detector.py:110` |
| RRF constant is 60; pool multiplier is 5 | `hybrid_ranker.py:170-175` |
| `classify_query` returns three bands, not two | `hybrid_ranker.py:180-200` |
| **`session_chunks` DDL and `match_session_chunks` exist only in the database, not in any tracked file** | `sp_retriever.py:9-10` states this outright |
| **`retrieve_sp_chunks` asks the RPC for `match_count` rows across ALL doc types, then filters to Special Provision in Python** | `sp_retriever.py:40-53` |
| **The ranker already deduplicates continuation chunks sharing a `section_id`** | `hybrid_ranker.py:172-175` |
| Two SP closures disagree on the similarity floor | `review.py:345` vs `review.py:382` |

The second bold row is a live recall bug. A check asking for 8 Special Provision passages gets the top 8 chunks of *any* type in the project, then throws away everything that is not a Special Provision. On a project with a narrative and a key map, that routinely leaves 3 or 4 passages where 8 were requested. Task 4 fixes it.

The third bold row is a hazard for Layer 2. Once SP chunks carry `section_id`, the ranker's existing section deduplication would strip exactly the continuation chunks a multi-chunk table needs. Task 9 keeps pinned chunks outside that path.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `backend/migrations/010_session_chunks_hybrid.sql` | create | Capture existing DDL, add GIN index and keyword function |
| `backend/sql/local_setup.sql` | modify | Add `session_chunks` so a fresh local DB is complete |
| `backend/app/retrieval_langchain/sp_retriever.py` | modify | Caller-set floor, doc-type-correct recall |
| `backend/app/ingestion/session_chunker.py` | modify | Section-aware SP chunking, table captions |
| `backend/app/compliance/anchors.py` | create | Anchor extraction, pure, no I/O |
| `backend/app/compliance/check_retrieval.py` | create | Pinning, fusion wiring, budget split |
| `backend/app/retrieval/hybrid_ranker.py` | modify | Extract table-agnostic `fuse` |
| `backend/app/api/review.py` | modify | Both SP closures delegate to `check_retrieval` |
| `backend/tests/test_anchors.py` | create | Anchor extraction unit tests |
| `backend/tests/test_session_chunker_sections.py` | create | Section-aware chunking tests |
| `backend/tests/test_check_retrieval.py` | create | Pinning, budget, degradation |
| `backend/tests/test_hybrid_fuse.py` | create | Fusion arithmetic |
| `backend/tests/integration/test_local_retrieval.py` | create | Acceptance test against local Supabase |
| `backend/scripts/sp_retrieval_probe.py` | modify | Add anchor and pin columns |

Anchor extraction lives in its own module because it is a pure function over a
string with no database and no engine dependency, which makes it testable in
isolation and importable by the probe script without pulling in the retrieval
stack.

---

## Task 1: Local database setup and a tracked schema

**Files:**
- Create: `backend/migrations/010_session_chunks_hybrid.sql`
- Modify: `backend/sql/local_setup.sql`

**Interfaces:**
- Consumes: nothing.
- Produces: SQL function `keyword_search_session_chunks(search_query text, p_session_id uuid, p_doc_type text, match_count int)` returning `(id, content, metadata, doc_type, rank)`.

`session_chunks` and `match_session_chunks` are not defined in any tracked file. Capture what exists before adding to it, so the local container and any future environment can be rebuilt.

- [ ] **Step 1: Dump the existing definitions from the local container**

Open the local Supabase Studio SQL editor and run:

```sql
select table_name, column_name, data_type
from information_schema.columns
where table_name = 'session_chunks'
order by ordinal_position;

select pg_get_functiondef(oid)
from pg_proc
where proname = 'match_session_chunks';
```

Paste both results into the new migration file as a comment block headed `-- Existing, captured 2026-09-14 --`, then write the `create table if not exists` and `create or replace function` statements that reproduce them. **Do not invent columns.** If the dump disagrees with anything this plan assumes, stop and report it.

- [ ] **Step 2: Write the migration**

Create `backend/migrations/010_session_chunks_hybrid.sql`:

```sql
-- 010: hybrid retrieval support for session_chunks.
--
-- session_chunks and match_session_chunks predate the migrations directory and
-- were applied straight to the database; sp_retriever.py's docstring says so.
-- Step 1 of this file captures them so the schema is reproducible, then adds
-- what BM25 retrieval needs.
--
-- Idempotent. Safe to re-run.

-- ── Captured existing definitions (see Step 1 of the plan) ──────────────────
-- <paste the information_schema and pg_get_functiondef output here, then the
--  create table / create function statements that reproduce them>

-- ── New: full-text index for BM25-style keyword search ─────────────────────
create index if not exists session_chunks_content_fts
  on session_chunks
  using gin(to_tsvector('english', content));

-- Metadata lookups drive the exact anchor layer; without this every pin is a
-- sequential scan of the project's chunks.
create index if not exists session_chunks_metadata_gin
  on session_chunks
  using gin(metadata jsonb_path_ops);

-- ── New: keyword search, mirroring keyword_search_chunks ───────────────────
-- ts_rank_cd is cover-density ranking, which approximates BM25. Filtering by
-- doc_type inside the function matters: the caller must not fetch N rows of
-- any type and then discard the ones it did not want (see Task 4).
create or replace function keyword_search_session_chunks(
  search_query  text,
  p_session_id  uuid,
  p_doc_type    text default null,
  match_count   int  default 8
)
returns table (
  id        uuid,
  content   text,
  metadata  jsonb,
  doc_type  text,
  rank      float8
)
language sql stable
as $$
  select
    session_chunks.id,
    session_chunks.content,
    session_chunks.metadata,
    session_chunks.doc_type,
    ts_rank_cd(
      to_tsvector('english', session_chunks.content),
      websearch_to_tsquery('english', search_query)
    )::float8 as rank
  from session_chunks
  where
    session_chunks.session_id = p_session_id
    and (p_doc_type is null or session_chunks.doc_type = p_doc_type)
    and to_tsvector('english', session_chunks.content)
          @@ websearch_to_tsquery('english', search_query)
  order by rank desc
  limit match_count;
$$;
```

- [ ] **Step 3: Apply it to the local container**

Neither env file defines `DATABASE_URL`, so `deploy_sql.py` cannot connect. Paste the file into the local Supabase Studio SQL editor and run it.

- [ ] **Step 4: Verify the function exists and that tokenisation survives a section number**

This is the single riskiest assumption in the whole plan. Run in the same editor:

```sql
select websearch_to_tsquery('english', '105.05 TABLE 105.05-1')::text;
```

Expected: a tsquery that still contains the numeric tokens. **If Postgres splits `105.05-1` into unusable fragments, say so and stop.** The BM25 layer depends on this and the rest of the plan would need rethinking.

- [ ] **Step 5: Mirror the additions into local_setup.sql**

Append the same `session_chunks` table, both indexes, `match_session_chunks` and `keyword_search_session_chunks` to `backend/sql/local_setup.sql`, so a fresh local database is complete from that one file. Keep its existing numbered-comment style.

- [ ] **Step 6: Commit**

```bash
git add backend/migrations/010_session_chunks_hybrid.sql backend/sql/local_setup.sql
git commit -m "feat(sql): track session_chunks schema and add hybrid search support"
```

---

## Task 2: Point the worktree at the local database

**Files:**
- Create: `backend/tests/integration/__init__.py`, `backend/tests/integration/conftest.py`
- Modify: `backend/tests/conftest.py` if one exists, else create it

**Interfaces:**
- Produces: pytest marker `integration`, and fixture `local_db` returning a Supabase client bound to the local container, skipping when unavailable.

The env files are gitignored and therefore absent from the worktree.

- [ ] **Step 1: Bring the env files into the worktree**

```bash
cp ../../../backend/.env backend/.env
cp ../../../backend/local.env.local backend/local.env.local
```

`backend/.gitignore` is a blanket `*`, so neither can be committed by accident. **Do not open or print either file.**

- [ ] **Step 2: Write the fixture**

Create `backend/tests/integration/conftest.py`:

```python
"""Integration fixtures. These talk to the LOCAL Supabase container only.

backend/local.env.local supplies SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY for
the local container but carries no OPENAI_API_KEY, so .env is loaded first and
local.env.local overlaid on top: local database, real embedding key.

Every test here is skipped when the container is not reachable, so the default
suite stays hermetic.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

_BACKEND = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

load_dotenv(_BACKEND / ".env")
load_dotenv(_BACKEND / "local.env.local", override=True)


@pytest.fixture(scope="session")
def local_db():
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        pytest.skip("local Supabase env not configured")
    if "localhost" not in url and "127.0.0.1" not in url and "kong" not in url:
        pytest.skip(f"refusing to run integration tests against a non-local host")
    from supabase import create_client
    client = create_client(url, key)
    try:
        client.table("session_chunks").select("id", count="exact", head=True).limit(1).execute()
    except Exception as exc:
        pytest.skip(f"local Supabase not reachable: {type(exc).__name__}")
    return client
```

The host guard matters. It is what makes it impossible for this suite to touch hosted data by a misconfigured variable.

- [ ] **Step 3: Register the marker**

Add to `backend/pytest.ini`, or to the `[tool.pytest.ini_options]` table if the project uses `pyproject.toml`. Check which exists first:

```bash
ls backend/pytest.ini backend/pyproject.toml backend/setup.cfg 2>/dev/null
```

```ini
[pytest]
markers =
    integration: needs the local Supabase container
```

- [ ] **Step 4: Verify the fixture connects**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/integration -q
```

Expected: `no tests ran`, and no error. A skip message naming the container is also fine and tells you the guard works.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/integration
git commit -m "test: local-only integration fixture for the Supabase container"
```

---

## Task 3: Extract a table-agnostic fusion helper

**Files:**
- Modify: `backend/app/retrieval/hybrid_ranker.py`
- Test: `backend/tests/test_hybrid_fuse.py` (create)

**Interfaces:**
- Produces: `fuse(vector_rows, keyword_rows, v_weight, k_weight, match_count, key="id") -> list[dict]` — pure, no database, no knowledge of which table the rows came from. Each returned row carries `similarity` set to its RRF score.

Pure extraction. `HybridRanker.search` must keep behaving identically, because the chat path depends on it.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_hybrid_fuse.py`:

```python
"""backend/tests/test_hybrid_fuse.py

Reciprocal Rank Fusion arithmetic, isolated from any database so it can be
reasoned about directly. RRF score for a document present in both lists:

    v_weight/(60 + rank_v) + k_weight/(60 + rank_k)

with 1-based ranks and a missing list contributing nothing.

    python -m pytest backend/tests/test_hybrid_fuse.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.retrieval.hybrid_ranker import fuse  # noqa: E402


def _row(rid):
    return {"id": rid, "content": f"body {rid}", "metadata": {}}


def test_a_document_in_both_lists_outranks_one_in_only_the_first():
    both, vector_only = _row("a"), _row("b")
    out = fuse([vector_only, both], [both], v_weight=0.5, k_weight=0.5, match_count=2)
    assert [r["id"] for r in out] == ["a", "b"]


def test_keyword_weight_can_lift_a_keyword_only_hit_above_a_vector_hit():
    # The anchored case: 0.3/0.7 is what classify_query returns for a query
    # carrying a section number, and it must actually change the order.
    v_first, k_first = _row("v"), _row("k")
    out = fuse([v_first], [k_first], v_weight=0.3, k_weight=0.7, match_count=2)
    assert out[0]["id"] == "k"


def test_similarity_carries_the_rrf_score_and_match_count_truncates():
    out = fuse([_row("a"), _row("b"), _row("c")], [], v_weight=1.0, k_weight=0.0, match_count=2)
    assert len(out) == 2
    assert out[0]["similarity"] > out[1]["similarity"]
    assert abs(out[0]["similarity"] - 1.0 / 61) < 1e-9


def test_empty_inputs_are_not_an_error():
    assert fuse([], [], v_weight=0.5, k_weight=0.5, match_count=5) == []


if __name__ == "__main__":
    test_a_document_in_both_lists_outranks_one_in_only_the_first()
    test_keyword_weight_can_lift_a_keyword_only_hit_above_a_vector_hit()
    test_similarity_carries_the_rrf_score_and_match_count_truncates()
    test_empty_inputs_are_not_an_error()
    print("All tests passed!")
```

- [ ] **Step 2: Run it to make sure it fails**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_hybrid_fuse.py -q
```

Expected: FAIL with `ImportError: cannot import name 'fuse'`.

- [ ] **Step 3: Read the existing merge code first**

```bash
rg --no-ignore -n "_RRF_K" -A 40 backend/app/retrieval/hybrid_ranker.py
```

Find the block inside `search` that builds the RRF scores. **Move that arithmetic into `fuse` unchanged.** Do not take the opportunity to improve it; behaviour must be identical.

- [ ] **Step 4: Write the function and call it from search**

Add to `backend/app/retrieval/hybrid_ranker.py`:

```python
def fuse(
    vector_rows: List[Dict[str, Any]],
    keyword_rows: List[Dict[str, Any]],
    v_weight: float,
    k_weight: float,
    match_count: int,
    key: str = "id",
) -> List[Dict[str, Any]]:
    """Reciprocal Rank Fusion over two ranked lists.

        rrf(d) = v_weight/(k + rank_v(d)) + k_weight/(k + rank_k(d))

    with k = _RRF_K, 1-based ranks, and an absent list contributing nothing.

    Table-agnostic on purpose: it knows nothing about `chunks` or
    `session_chunks`, only about two ranked lists of dicts sharing an id
    field. The returned rows carry their RRF score in ``similarity`` so a
    caller can apply a threshold.
    """
    scores: Dict[Any, float] = {}
    rows_by_id: Dict[Any, Dict[str, Any]] = {}
    for rows, weight in ((vector_rows, v_weight), (keyword_rows, k_weight)):
        for rank, row in enumerate(rows, start=1):
            rid = row.get(key)
            rows_by_id.setdefault(rid, row)
            scores[rid] = scores.get(rid, 0.0) + weight / (_RRF_K + rank)
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])[:match_count]
    return [{**rows_by_id[rid], "similarity": score} for rid, score in ordered]
```

Then replace the inline merge inside `HybridRanker.search` with a call to it, keeping the debug-mode rank annotations that method already adds.

- [ ] **Step 5: Run the fusion tests, then the whole suite**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_hybrid_fuse.py -q
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q
```

Expected: `4 passed`, then `197 passed`. Any existing ranker test that changes behaviour means the extraction was not pure. Fix the extraction, not the test.

- [ ] **Step 6: Commit**

```bash
git add backend/app/retrieval/hybrid_ranker.py backend/tests/test_hybrid_fuse.py
git commit -m "refactor(retrieval): extract table-agnostic RRF fusion from HybridRanker"
```

---

## Task 4: Fix Special Provision recall and the threshold split

**Files:**
- Modify: `backend/app/retrieval_langchain/sp_retriever.py`
- Test: `backend/tests/test_sp_search_parity.py` (create)

**Interfaces:**
- Produces: `retrieve_sp_chunks(db, embed_fn, project_id, query, match_count=8, match_threshold=0.0, doc_type="special_provision")`.

Two bugs, one function. The retriever asks for `match_count` rows across every document type in the project and then discards non-Special-Provision rows in Python, so a check asking for 8 passages can receive 3. Separately the 0.2 floor applies on one code path and not the other.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_sp_search_parity.py`:

```python
"""backend/tests/test_sp_search_parity.py

Two bugs this file pins down.

1. The RPC returns the top match_count rows of ANY doc_type for the session,
   and the caller then filtered to special_provision in Python. On a project
   with a narrative and a key map, asking for 8 SP passages routinely yielded
   3. Over-fetch so the post-filter still leaves match_count.

2. The fresh-review closure applied no similarity floor and the rerun closure
   applied 0.2, so the same check retrieved differently depending on which
   code path ran. The floor is now the caller's choice, defaulting to none.

    python -m pytest backend/tests/test_sp_search_parity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.retrieval_langchain.sp_retriever import retrieve_sp_chunks  # noqa: E402


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.last_params = None

    def rpc(self, name, params):
        self.last_params = params
        outer = self

        class _R:
            def execute(self_inner):
                return type("X", (), {"data": outer.rows})()

        return _R()


def _mixed_rows(n_sp, n_other):
    sp = [{"content": f"sp {i}", "doc_type": "special_provision", "similarity": 0.9} for i in range(n_sp)]
    other = [{"content": f"nar {i}", "doc_type": "narrative", "similarity": 0.8} for i in range(n_other)]
    return other + sp   # interleaving order does not matter; the filter does


def test_over_fetches_so_the_doc_type_filter_still_yields_match_count():
    db = _FakeDB(_mixed_rows(n_sp=8, n_other=24))
    rows = retrieve_sp_chunks(db, lambda q: [0.0] * 3, "proj-1", "night work", match_count=8)
    assert db.last_params["match_count"] > 8, "must over-fetch to survive the doc_type filter"
    assert len(rows) == 8
    assert all(r["doc_type"] == "special_provision" for r in rows)


def test_defaults_to_no_similarity_floor():
    db = _FakeDB([])
    retrieve_sp_chunks(db, lambda q: [0.0] * 3, "proj-1", "q")
    assert db.last_params["match_threshold"] == 0.0


def test_honours_an_explicit_floor():
    db = _FakeDB([])
    retrieve_sp_chunks(db, lambda q: [0.0] * 3, "proj-1", "q", match_threshold=0.35)
    assert db.last_params["match_threshold"] == 0.35


if __name__ == "__main__":
    test_over_fetches_so_the_doc_type_filter_still_yields_match_count()
    test_defaults_to_no_similarity_floor()
    test_honours_an_explicit_floor()
    print("All tests passed!")
```

- [ ] **Step 2: Run it to make sure it fails**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_sp_search_parity.py -q
```

Expected: three failures. The first on `match_count > 8`, the others on the hardcoded `_MATCH_THRESHOLD` and the missing keyword.

- [ ] **Step 3: Rewrite the function**

In `backend/app/retrieval_langchain/sp_retriever.py`:

```python
# Retrieval used to drop every chunk below 0.2 cosine. For chat that is a
# reasonable floor; for a compliance check it is not, because the query is a
# 150-word rule that embeds far from a 600-token clause even when that clause
# is the right one. Below the floor the check received no SP text at all and
# answered from its other sources, silently. Callers now choose.
_DEFAULT_MATCH_THRESHOLD = 0.0

# match_session_chunks ranks across every doc_type in the session, so a
# post-filter to one type throws rows away. Ask for enough that the survivors
# still fill the caller's budget.
_DOC_TYPE_OVERFETCH = 4


def retrieve_sp_chunks(
    db: Any,
    embed_fn: Callable[[str], List[float]],
    project_id: str,
    query: str,
    match_count: int = 8,
    match_threshold: float = _DEFAULT_MATCH_THRESHOLD,
    doc_type: str = "special_provision",
) -> List[Dict[str, Any]]:
    """Vector-search this project's Special Provision chunks.

    Returns raw RPC rows (``content``, ``doc_type``, ``metadata``,
    ``similarity``) -- the shape ``session_query`` already consumes.
    """
    embedding = embed_fn(query)
    rows = (
        db.rpc(
            "match_session_chunks",
            {
                "query_embedding": embedding,
                "p_session_id": project_id,
                "match_count": match_count * _DOC_TYPE_OVERFETCH,
                "match_threshold": match_threshold,
            },
        )
        .execute()
        .data
    ) or []
    return [r for r in rows if r.get("doc_type") == doc_type][:match_count]
```

- [ ] **Step 4: Decide what every other caller should do**

```bash
rg --no-ignore -n "retrieve_sp_chunks" backend/app backend/scripts
```

Chat callers keep today's behaviour by passing `match_threshold=0.2` explicitly, with a one-line comment saying why. Do not leave any caller's behaviour changed by accident.

- [ ] **Step 5: Run the suite and commit**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q
git add backend/app/retrieval_langchain/sp_retriever.py backend/tests/test_sp_search_parity.py
git commit -m "fix(retrieval): over-fetch past the doc_type filter and let callers set the floor"
```

---

## Task 5: Anchor extraction

**Files:**
- Create: `backend/app/compliance/anchors.py`
- Test: `backend/tests/test_anchors.py` (create)

**Interfaces:**
- Produces:
  - `extract_anchors(instruction: str) -> Anchors` where `Anchors` is a frozen dataclass with `sections: tuple[str, ...]` and `tables: tuple[str, ...]`
  - `Anchors.is_empty` property
  - `Anchors.as_query() -> str` — the short BM25 query string

Pure, no I/O. Reads only the instruction's first paragraph, which is where the v3 rewrite puts the anchors.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_anchors.py`:

```python
"""backend/tests/test_anchors.py

Anchor extraction from a check instruction. Anchors do two jobs: they are the
exact keys for the metadata lookup, and they are the BM25 query. The BM25 part
is why only the first paragraph is read -- websearch_to_tsquery AND-chains
every token, so feeding it a 150-word rule matches nothing.

    python -m pytest backend/tests/test_anchors.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.compliance.anchors import extract_anchors  # noqa: E402


def test_extracts_subsection_and_sub_subsection_numbers():
    a = extract_anchors(
        "Special Provisions 105.07.01 Working in the Vicinity of Utilities and "
        "105.07.02 Work Performed by Utilities: \"Advance Notice Requirements\".\n\n"
        "Utilities on the Key Sheet sit within project limits."
    )
    assert a.sections == ("105.07.01", "105.07.02")


def test_extracts_a_table_caption():
    a = extract_anchors(
        "Special Provisions 105.05 WORKING DRAWINGS: \"TABLE 105.05-1 IS CHANGED TO\", "
        "\"Working Drawing Submission Category\", Certified and Approved columns."
    )
    assert a.sections == ("105.05",)
    assert a.tables == ("TABLE 105.05-1",)


def test_extracts_a_section_heading():
    a = extract_anchors(
        "Special Provisions SECTION 702 - TRAFFIC SIGNALS and SECTION 703 - HIGHWAY LIGHTING."
    )
    assert a.sections == ("SECTION 702", "SECTION 703")


def test_reads_only_the_first_paragraph():
    # A number mentioned deep in the rule body is not a retrieval anchor; the
    # first paragraph is where the v3 template puts the ones that are.
    a = extract_anchors("Restricted window: Dec 15 - Mar 15.\n\nStd Spec 504.03.02.C requires a plan.")
    assert a.sections == ()


def test_an_instruction_with_no_anchor_is_empty_not_an_error():
    a = extract_anchors("Confirm the narrative addresses community commitments.")
    assert a.is_empty
    assert a.as_query() == ""


def test_as_query_is_short_and_carries_every_anchor():
    a = extract_anchors(
        "Special Provisions 105.05 WORKING DRAWINGS: \"TABLE 105.05-1 IS CHANGED TO\"."
    )
    q = a.as_query()
    assert "105.05" in q and "TABLE 105.05-1" in q
    assert len(q.split()) <= 12, "the BM25 query must stay short; tsquery ANDs every token"


def test_deduplicates_and_preserves_first_appearance_order():
    a = extract_anchors("105.07 and 105.07.02 and 105.07 again.")
    assert a.sections == ("105.07", "105.07.02")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("All tests passed!")
```

- [ ] **Step 2: Run it to make sure it fails**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_anchors.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.compliance.anchors'`.

- [ ] **Step 3: Write the module**

Create `backend/app/compliance/anchors.py`:

```python
"""Retrieval anchors extracted from a check instruction.

An anchor is a section or table the check names explicitly: "105.07.02",
"SECTION 703", "TABLE 105.05-1". Anchors do two jobs.

1. They are exact keys for the metadata lookup in check_retrieval, which pins
   the named clause into the evidence with no ranking involved.
2. They are the BM25 query. That is why only the FIRST PARAGRAPH is read:
   websearch_to_tsquery AND-chains every surviving token, so handing it a
   150-word rule body matches zero rows. The v3 instruction template puts the
   anchors in the opening sentence for exactly this reason.

Pure: no database, no engine import, so the probe script can use it directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Tuple

# Mirrors app.ingestion.section_detector's dialect rather than inventing a
# second one: 105.07, 105.07.02, and "SECTION 703".
_SECTION_RE = re.compile(r"\b\d{3,4}\.\d{2}(?:\.\d{2})?\b|\bSECTION\s+\d{3}\b", re.IGNORECASE)
_TABLE_RE = re.compile(r"\bTABLE\s+[\dA-Z]+(?:\.[\dA-Z]+)*(?:-\d+)?\b", re.IGNORECASE)


@dataclass(frozen=True)
class Anchors:
    sections: Tuple[str, ...] = ()
    tables: Tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.sections and not self.tables

    def as_query(self) -> str:
        """The BM25 query: anchors only, never the rule body."""
        return " ".join((*self.sections, *self.tables))


def _dedupe(values) -> Tuple[str, ...]:
    seen, out = set(), []
    for v in values:
        norm = v.upper() if v.upper().startswith(("SECTION", "TABLE")) else v
        norm = re.sub(r"\s+", " ", norm).strip()
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return tuple(out)


def extract_anchors(instruction: str) -> Anchors:
    """Pull section and table anchors from an instruction's first paragraph."""
    head = (instruction or "").split("\n\n", 1)[0]
    tables = _dedupe(_TABLE_RE.findall(head))
    # A table caption contains a section-shaped number; do not report it twice.
    table_blob = " ".join(tables)
    sections = _dedupe(s for s in _SECTION_RE.findall(head) if s not in table_blob)
    return Anchors(sections=sections, tables=tables)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_anchors.py -q
```

Expected: `7 passed`.

- [ ] **Step 5: See what the real catalog yields**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0, 'backend')
from app.compliance.catalog import BUILTIN_CHECKS
from app.compliance.anchors import extract_anchors
sp = [c for c in BUILTIN_CHECKS if 'sp' in c.source_files and c.check_type == 'llm']
hit = 0
for c in sp:
    a = extract_anchors(c.instruction)
    if not a.is_empty: hit += 1
    print(f'{c.check_key:<38} {a.as_query() or \"(none)\"}')
print(f'\n{hit}/{len(sp)} sp checks yield an anchor')
"
```

Record the hit rate. **This is the baseline for whether the v3 anchor-first rewrite is worth doing**, and it is a real finding either way. Report it before moving on.

- [ ] **Step 6: Commit**

```bash
git add backend/app/compliance/anchors.py backend/tests/test_anchors.py
git commit -m "feat(compliance): extract retrieval anchors from check instructions"
```

---

## Task 6: Section-aware Special Provision chunking

**Files:**
- Modify: `backend/app/ingestion/session_chunker.py`
- Test: `backend/tests/test_session_chunker_sections.py` (create)

**Interfaces:**
- Consumes: `app.ingestion.section_detector.detect`.
- Produces: `chunk_special_provision` returns chunks whose metadata additionally carries `section_id: str | None`, `section_title: str | None`, `tables: list[str]`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_session_chunker_sections.py`:

```python
"""backend/tests/test_session_chunker_sections.py

Special Provision chunks used to carry only doc_type, page_pdf and
chunk_index, cut wherever a 600-token window landed, so a chunk routinely
began inside 105.05 and ended inside 105.07 and nothing knew which clause any
passage belonged to. section_detector already recognises every NJDOT heading
form and already feeds the static specs ingestion; it was never pointed at
per-project uploads.

    python -m pytest backend/tests/test_session_chunker_sections.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.ingestion.session_chunker import chunk_special_provision  # noqa: E402


def _pages(*texts):
    return [{"page_num": i + 1, "text": t} for i, t in enumerate(texts)]


def test_each_chunk_carries_the_section_it_belongs_to():
    pages = _pages(
        "105.05 WORKING DRAWINGS\n"
        "Submit working drawings as specified. TABLE 105.05-1 IS CHANGED TO the following.\n"
        "105.06 COOPERATION WITH OTHERS\n"
        "Cooperate with other contractors working within the project limits.\n"
    )
    chunks = chunk_special_provision(pages)
    by_section = {c["metadata"].get("section_id") for c in chunks}
    assert "105.05" in by_section
    assert "105.06" in by_section


def test_a_chunk_never_spans_two_detected_sections():
    pages = _pages(
        "105.05 WORKING DRAWINGS\nFirst clause body.\n"
        "105.06 COOPERATION WITH OTHERS\nSecond clause body.\n"
    )
    for c in chunk_special_provision(pages):
        assert "105.06 COOPERATION" not in c["content"] or c["metadata"]["section_id"] == "105.06"


def test_table_captions_are_recorded_in_metadata():
    pages = _pages("105.05 WORKING DRAWINGS\nTABLE 105.05-1 IS CHANGED TO the following.\n")
    chunks = chunk_special_provision(pages)
    assert any("TABLE 105.05-1" in c["metadata"].get("tables", []) for c in chunks)


def test_section_title_is_captured():
    chunks = chunk_special_provision(_pages("105.05 WORKING DRAWINGS\nBody text here.\n"))
    assert any(c["metadata"].get("section_title") == "WORKING DRAWINGS" for c in chunks)


def test_text_before_any_heading_still_becomes_a_chunk_with_no_section():
    chunks = chunk_special_provision(_pages("Cover page boilerplate with no heading at all.\n"))
    assert chunks
    assert chunks[0]["metadata"].get("section_id") is None


def test_existing_metadata_fields_are_preserved():
    chunks = chunk_special_provision(_pages("105.05 WORKING DRAWINGS\nBody.\n"))
    m = chunks[0]["metadata"]
    assert m["doc_type"] == "special_provision"
    assert "page_pdf" in m and "chunk_index" in m


def test_a_long_section_still_splits_into_overlapping_windows():
    body = " ".join(f"word{i}" for i in range(4000))
    chunks = chunk_special_provision(_pages(f"105.05 WORKING DRAWINGS\n{body}\n"))
    assert len(chunks) > 1
    assert all(c["metadata"]["section_id"] == "105.05" for c in chunks)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("All tests passed!")
```

- [ ] **Step 2: Run it to make sure it fails**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_session_chunker_sections.py -q
```

Expected: failures on every metadata assertion, because the chunker emits only three fields.

- [ ] **Step 3: Read the current implementation end to end**

```bash
rg --no-ignore -n "def chunk_special_provision" -A 85 backend/app/ingestion/session_chunker.py
```

Note `_SP_MAX_TOKENS` is 600, `_SP_OVERLAP` is 100, and the existing flow is: filter pages, extract tables when a pdf path is supplied, detect boilerplate, build one flat token stream, slide a window over it. **Keep all of that.** The change is to segment before windowing.

- [ ] **Step 4: Rewrite the chunking body**

Replace the flat token stream with a per-section one. Keep `_page_at` and the boilerplate handling exactly as they are.

```python
_TABLE_CAPTION_RE = re.compile(r"\bTABLE\s+[\dA-Z]+(?:\.[\dA-Z]+)*(?:-\d+)?\b", re.IGNORECASE)


def _segment_by_section(pages, boilerplate):
    """Split cleaned page text into (section_id, section_title, lines, page) runs.

    A run starts at each line section_detector recognises as a heading and ends
    at the next one. Text before the first heading becomes a run with no
    section, which is normal for cover pages and preambles.
    """
    from app.ingestion.section_detector import detect

    runs = []
    cur = {"section_id": None, "section_title": None, "lines": [], "page": 1}
    for page in pages:
        for line in page["text"].splitlines():
            if line.strip() in boilerplate:
                continue
            match = detect(line)
            if match:
                if cur["lines"]:
                    runs.append(cur)
                cur = {
                    "section_id": match["section_id"],
                    "section_title": match["title"],
                    "lines": [line],
                    "page": page["page_num"],
                }
            else:
                if not cur["lines"]:
                    cur["page"] = page["page_num"]
                cur["lines"].append(line)
    if cur["lines"]:
        runs.append(cur)
    return runs
```

Then window each run independently, carrying its identity onto every chunk it produces:

```python
    runs = _segment_by_section(pages, boilerplate)
    chunks: List[Dict[str, Any]] = []
    idx = 0
    for run in runs:
        text = "\n".join(run["lines"]).strip()
        if not text:
            continue
        tokens = enc.encode(text)
        start = 0
        while start < len(tokens):
            end = min(start + _SP_MAX_TOKENS, len(tokens))
            body = enc.decode(tokens[start:end]).strip()
            if body:
                chunks.append({
                    "content": body,
                    "metadata": {
                        "doc_type": "special_provision",
                        "page_pdf": run["page"],
                        "chunk_index": idx,
                        "section_id": run["section_id"],
                        "section_title": run["section_title"],
                        "tables": sorted({m.group(0).upper() for m in _TABLE_CAPTION_RE.finditer(body)}),
                    },
                })
                idx += 1
            if end >= len(tokens):
                break
            start = end - _SP_OVERLAP
    return chunks
```

`_page_at` is no longer reachable if nothing else uses it. Check before deleting:

```bash
rg --no-ignore -n "_page_at" backend/app
```

- [ ] **Step 5: Run the new tests, then the whole suite**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_session_chunker_sections.py -q
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q
```

Expected: `7 passed`, then `211 passed`. If an existing chunker test asserts a chunk count, read it: a different count is expected now and the assertion should move to asserting section integrity instead.

- [ ] **Step 6: Commit**

```bash
git add backend/app/ingestion/session_chunker.py backend/tests/test_session_chunker_sections.py
git commit -m "feat(ingestion): split Special Provision chunks on section boundaries"
```

---

## Task 7: Pinning, fusion and the budget split

**Files:**
- Create: `backend/app/compliance/check_retrieval.py`
- Test: `backend/tests/test_check_retrieval.py` (create)

**Interfaces:**
- Consumes: `extract_anchors` (Task 5), `fuse` (Task 3), `retrieve_sp_chunks` (Task 4).
- Produces:
  - `PIN_BUDGET_FRACTION = 0.5`
  - `pin_by_anchors(db, project_id, anchors, doc_type, limit) -> list[dict]`
  - `project_has_section_metadata(db, project_id, doc_type) -> bool`
  - `retrieve_for_check(db, embed_fn, project_id, instruction, top_k, doc_type) -> RetrievalResult`
  - `@dataclass RetrievalResult(rows: list[dict], pinned: int, anchors: Anchors, anchor_missing: bool)`

`anchor_missing` is true only when the project *has* section metadata and the anchor still matched nothing, which is the case worth surfacing. A project ingested before Task 6 has no metadata at all and degrades silently.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_check_retrieval.py`:

```python
"""backend/tests/test_check_retrieval.py

Composition of the three retrieval layers, with fakes for the database so the
logic is testable without a container.

    python -m pytest backend/tests/test_check_retrieval.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.compliance.check_retrieval import (  # noqa: E402
    PIN_BUDGET_FRACTION, retrieve_for_check,
)

_INSTRUCTION = (
    "Special Provisions 105.05 WORKING DRAWINGS: \"TABLE 105.05-1 IS CHANGED TO\".\n\n"
    "Classify each submittal by the governing table."
)


def _chunk(cid, section=None, tables=(), body="body"):
    return {
        "id": cid, "content": body, "doc_type": "special_provision",
        "metadata": {"section_id": section, "tables": list(tables)}, "similarity": 0.5,
    }


class _FakeDB:
    """Enough of the PostgREST surface for pinning plus the two searches."""

    def __init__(self, pinned=(), vector=(), keyword=()):
        self._pinned, self._vector, self._keyword = list(pinned), list(vector), list(keyword)
        self.keyword_query = None

    def rpc(self, name, params):
        outer = self
        if name == "keyword_search_session_chunks":
            outer.keyword_query = params["search_query"]
            data = outer._keyword
        else:
            data = outer._vector

        class _R:
            def execute(self_inner):
                return type("X", (), {"data": data})()

        return _R()

    def table(self, _name):
        return _FakeTable(self._pinned)


class _FakeTable:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def not_(self, *a, **k): return self
    def is_(self, *a, **k): return self
    def or_(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def order(self, *a, **k): return self
    def execute(self):
        return type("X", (), {"data": self.rows, "count": len(self.rows)})()


def test_an_anchored_chunk_is_pinned_first():
    db = _FakeDB(
        pinned=[_chunk("t", section="105.05", tables=["TABLE 105.05-1"])],
        vector=[_chunk("v1"), _chunk("v2")],
        keyword=[_chunk("k1")],
    )
    out = retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=8)
    assert out.rows[0]["id"] == "t"
    assert out.pinned == 1
    assert out.anchor_missing is False


def test_the_bm25_query_is_anchors_only_never_the_rule_body():
    db = _FakeDB(pinned=[], vector=[_chunk("v1")], keyword=[])
    retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=8)
    assert "105.05" in db.keyword_query
    assert "Classify each submittal" not in db.keyword_query
    assert len(db.keyword_query.split()) <= 12


def test_pinned_results_are_capped_so_they_cannot_fill_the_budget():
    many = [_chunk(f"t{i}", section="105.05") for i in range(20)]
    db = _FakeDB(pinned=many, vector=[_chunk(f"v{i}") for i in range(20)], keyword=[])
    out = retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=8)
    assert out.pinned <= int(8 * PIN_BUDGET_FRACTION)
    assert len(out.rows) == 8


def test_a_pinned_chunk_is_not_repeated_by_the_fused_half():
    shared = _chunk("t", section="105.05")
    db = _FakeDB(pinned=[shared], vector=[shared, _chunk("v1")], keyword=[shared])
    out = retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=8)
    assert [r["id"] for r in out.rows].count("t") == 1


def test_no_anchor_means_dense_only_and_no_keyword_call():
    db = _FakeDB(pinned=[], vector=[_chunk("v1")], keyword=[])
    out = retrieve_for_check(
        db, lambda q: [0.0] * 3, "p1", "Confirm the narrative addresses community commitments.", top_k=8,
    )
    assert db.keyword_query is None
    assert out.pinned == 0
    assert out.anchor_missing is False


def test_a_project_with_no_section_metadata_degrades_silently():
    # Ingested before section-aware chunking: pinning cannot work and that is
    # not evidence the clause is absent from the document.
    db = _FakeDB(pinned=[], vector=[_chunk("v1")], keyword=[_chunk("k1")])
    out = retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=8)
    assert out.anchor_missing is False
    assert out.rows


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("All tests passed!")
```

- [ ] **Step 2: Run it to make sure it fails**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_check_retrieval.py -q
```

Expected: `ModuleNotFoundError: No module named 'app.compliance.check_retrieval'`.

- [ ] **Step 3: Write the module**

Create `backend/app/compliance/check_retrieval.py`. Implement in this order so each test turns green in turn: `project_has_section_metadata`, `pin_by_anchors`, then `retrieve_for_check` composing pin, dense, keyword, fuse, dedupe, truncate.

Key decisions to encode, each with the comment explaining why:

- Pinned chunks are capped at `int(top_k * PIN_BUDGET_FRACTION)` and ordered by `chunk_index` so a multi-chunk table arrives in reading order.
- Pinned chunks bypass fusion entirely. The shared ranker deduplicates continuation chunks sharing a `section_id`, which is right for chat and wrong here, since a table spanning three chunks needs all three.
- The keyword leg is skipped when `anchors.is_empty`, and weights come from `classify_query(anchors.as_query())`.
- Dedupe fused rows against pinned ids before truncating to `top_k`.
- `anchor_missing` is `not anchors.is_empty and not pinned and project_has_section_metadata(...)`.

- [ ] **Step 4: Run the tests, then the whole suite**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_check_retrieval.py -q
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q
```

Expected: `6 passed`, then `217 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/compliance/check_retrieval.py backend/tests/test_check_retrieval.py
git commit -m "feat(compliance): pin anchored chunks and fuse the remainder"
```

---

## Task 8: Route both Special Provision closures through the new module

**Files:**
- Modify: `backend/app/api/review.py:345-415`
- Test: `backend/tests/test_eval_engine.py`

**Interfaces:**
- Consumes: `retrieve_for_check` (Task 7).
- Produces: no new public symbols. Both closures keep the `CitedSearch` signature `(query: str, top_k: int) -> tuple[str, dict[str, EvidenceCandidate]]`.

The two closures diverged once already and that divergence caused the run-to-run flapping. Collapsing them onto one implementation is the point of this task, not a side effect.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_eval_engine.py`:

```python
def test_both_sp_closures_return_the_same_passages_for_one_query():
    """The fresh-review closure applied no similarity floor and the rerun
    closure applied 0.2, so the same check retrieved differently depending on
    which code path ran. One implementation, one result."""
    from app.api.review import _build_sp_search_fn, _build_sp_search_fn_from_supabase
    # Build both against the same fake backing store and assert the returned
    # passage text matches. See the fakes in test_check_retrieval.py; reuse
    # them rather than writing a third set.
    ...
```

Fill in the body using the fakes from `test_check_retrieval.py`. Import them rather than copying.

- [ ] **Step 2: Run it to make sure it fails**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py -k sp_closures -q
```

- [ ] **Step 3: Delegate both closures**

Each closure keeps its own job of turning rows into tagged text and `EvidenceCandidate` entries, which is where they legitimately differ, since one holds chunks in process and the other reads Supabase. The retrieval itself moves to `retrieve_for_check`. Keep the `[cite:sp-N]` tagging and the `"\n\n---\n\n"` join exactly as they are, because the engine parses both.

The in-process closure has no database to pin against. Give it the same interface by pinning over its in-memory chunk list on the same metadata fields, so behaviour matches rather than merely resembling.

- [ ] **Step 4: Run the whole suite and commit**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q
git add backend/app/api/review.py backend/tests/test_eval_engine.py
git commit -m "fix(api): one retrieval implementation behind both SP closures"
```

---

## Task 9: Surface a genuinely missing anchor

**Files:**
- Modify: `backend/app/compliance/eval_engine.py`
- Test: `backend/tests/test_eval_engine.py`

**Interfaces:**
- Consumes: `RetrievalResult.anchor_missing` (Task 7).

A check that names Table 105.05-1 against a document that demonstrably does not contain it should say so rather than answer from the surrounding prose. This is the `insufficient_evidence` flag already on this branch, not a new state.

- [ ] **Step 1: Write the failing test**

```python
def test_a_named_anchor_absent_from_a_sectioned_document_reports_missing():
    """The project HAS section metadata, so pinning could have worked, and the
    named table still is not there. That is a real gap, unlike a project
    ingested before section-aware chunking, which must degrade silently."""
    check = _make_check(check_key="working_drawing_review_time", source_files=["sp", "schedule"])

    def _sp_missing_anchor(query, top_k=8):
        return "[cite:sp-0] unrelated clause text", {"sp-0": _candidate()}, True   # anchor_missing

    result, usage = _call_evaluate_one_check(check, _FakeStructuredLLM([]), _FakeStructuredLLM([]),
                                             sp_search_fn=_sp_missing_anchor)
    assert result.status == "Missing"
    assert "105.05" in result.evidence or "TABLE 105.05-1" in result.evidence
    assert usage["llm_call_count"] == 0
```

Adjust to whatever signature Task 8 settled on for carrying `anchor_missing` back. If threading a third return value through `CitedSearch` proves ugly, carry it on the retrieval log record from the v3 plan's Task 3 instead and read it there. **Pick one and write it down in the module docstring.**

- [ ] **Step 2: Run it, implement, run again**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/test_eval_engine.py -q
```

- [ ] **Step 3: Run the whole suite and commit**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q
git add backend/app/compliance/eval_engine.py backend/tests/test_eval_engine.py
git commit -m "feat(compliance): report Missing when a named anchor is absent from a sectioned document"
```

---

## Task 10: Acceptance test against the local container

**Files:**
- Create: `backend/tests/integration/test_local_retrieval.py`

**Interfaces:**
- Consumes: the `local_db` fixture (Task 2), `retrieve_for_check` (Task 7), `chunk_special_provision` (Task 6).

This is the test that cannot be faked. It is the only way to confirm that `websearch_to_tsquery` still matches a token like `105.05-1` after its own tokenisation, and that pinning is stable rather than merely usually right.

- [ ] **Step 1: Write the test**

```python
"""backend/tests/integration/test_local_retrieval.py

Local Supabase only. Seeds a synthetic Special Provision, then asserts the
behaviour the whole design exists to produce: a check naming TABLE 105.05-1
receives it, first, every single time.

    python -m pytest backend/tests/integration -q -m integration
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration

_SP_TEXT = """105.05 WORKING DRAWINGS
Submit working drawings to the Engineer. TABLE 105.05-1 IS CHANGED TO the following.
Working Drawing Submission Category: Certified 30 days, Approved 45 days.

105.06 COOPERATION WITH OTHERS
Cooperate with other contractors working within the project limits.

105.07.02 WORK PERFORMED BY UTILITIES
Provide advance notice prior to the start of gas work. Safe-time applies to
fiber optic cable splicing between 12:00am and 6:00am.
"""

_INSTRUCTION = (
    "Special Provisions 105.05 WORKING DRAWINGS: \"TABLE 105.05-1 IS CHANGED TO\", "
    "\"Working Drawing Submission Category\", Certified and Approved columns.\n\n"
    "Classify each submittal by the governing table, not the WBS folder."
)


@pytest.fixture(scope="module")
def seeded_project(local_db):
    from app.ingestion.session_chunker import chunk_special_provision
    from app.ingestion.chunk_store import insert_session_chunks
    from langchain_openai import OpenAIEmbeddings
    import app.config as config

    project_id = str(uuid.uuid4())
    pages = [{"page_num": 1, "text": _SP_TEXT}]
    chunks = chunk_special_provision(pages)
    emb = OpenAIEmbeddings(model=config.EMBEDDING_MODEL, api_key=config.OPENAI_API_KEY)
    vectors = emb.embed_documents([c["content"] for c in chunks])
    insert_session_chunks(local_db, project_id, [{**c, "embedding": v} for c, v in zip(chunks, vectors)])
    yield project_id, emb
    local_db.table("session_chunks").delete().eq("session_id", project_id).execute()


def test_section_metadata_survives_the_round_trip(local_db, seeded_project):
    project_id, _ = seeded_project
    rows = local_db.table("session_chunks").select("metadata").eq("session_id", project_id).execute().data
    sections = {r["metadata"].get("section_id") for r in rows}
    assert {"105.05", "105.06", "105.07.02"} <= sections


def test_the_named_table_is_pinned_first_on_ten_consecutive_runs(local_db, seeded_project):
    from app.compliance.check_retrieval import retrieve_for_check

    project_id, emb = seeded_project
    firsts = []
    for _ in range(10):
        out = retrieve_for_check(local_db, emb.embed_query, project_id, _INSTRUCTION, top_k=8)
        assert out.pinned >= 1
        firsts.append("TABLE 105.05-1" in out.rows[0]["content"])
    assert all(firsts), "the named table must be first on every run, not most runs"


def test_websearch_tsquery_actually_matches_a_hyphenated_section_number(local_db, seeded_project):
    # The single riskiest assumption in the design. If Postgres tokenisation
    # mangles "105.05-1", BM25 contributes nothing and only pinning works.
    project_id, _ = seeded_project
    rows = local_db.rpc("keyword_search_session_chunks", {
        "search_query": "105.05 TABLE 105.05-1",
        "p_session_id": project_id,
        "p_doc_type": "special_provision",
        "match_count": 8,
    }).execute().data
    assert rows, "keyword search returned nothing for a literal section anchor"
    assert any("TABLE 105.05-1" in r["content"] for r in rows)


def test_a_phrase_anchor_is_found_by_the_keyword_leg(local_db, seeded_project):
    from app.compliance.check_retrieval import retrieve_for_check

    project_id, emb = seeded_project
    out = retrieve_for_check(
        local_db, emb.embed_query, project_id,
        "Special Provisions 105.07.02: \"safe-time\", \"fiber optic cable splicing\".\n\nCheck night work.",
        top_k=8,
    )
    assert any("safe-time" in r["content"] for r in out.rows)
```

- [ ] **Step 2: Run it against the local container**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests/integration -q -m integration
```

Expected: `4 passed`. **If the tsquery test fails, stop and report it.** The design's third layer rests on it, and pinning would have to carry the load alone.

- [ ] **Step 3: Confirm the default suite still skips them**

```bash
PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q
```

Expected: the integration tests skip or deselect, and the hermetic count is unchanged.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/integration/test_local_retrieval.py
git commit -m "test: acceptance test for anchored retrieval against local Supabase"
```

---

## Task 11: Measure before and after

**Files:**
- Modify: `backend/scripts/sp_retrieval_probe.py` (created by the v3 plan's Task 4)
- Create: `docs/superpowers/plans/2026-09-14-hybrid-retrieval-results.md`

- [ ] **Step 1: Add anchor and pin columns to the probe**

For each `sp` check, additionally report `anchor_query` from `extract_anchors`, `pinned_count`, and `anchor_in_top_passage`. Reuse `app.compliance.anchors`; do not re-implement the regexes.

- [ ] **Step 2: Capture a baseline on a pre-change project**

A project ingested before Task 6 has no section metadata, which is exactly the baseline.

```bash
cd backend && PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe scripts/sp_retrieval_probe.py --project-id <id> --label before-hybrid
```

- [ ] **Step 3: Re-ingest the same Special Provision and capture the after**

```bash
cd backend && PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe scripts/sp_retrieval_probe.py --project-id <new-id> --label after-hybrid
cd backend && PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe scripts/sp_retrieval_probe.py --compare before-hybrid after-hybrid
```

- [ ] **Step 4: Write the results against the design's success criteria**

| Criterion | Result |
|---|---|
| A named section or table is received whenever the document contains it | |
| Flapping checks return identical verdicts across three consecutive runs | |
| Anchored checks show higher top similarity than baseline | |
| Chat path unchanged | |

State plainly which layers moved their own metric and which did not. A layer that did not earn its place should be named, not folded into an aggregate improvement.

- [ ] **Step 5: Commit**

```bash
git add backend/scripts/sp_retrieval_probe.py docs/superpowers/plans/2026-09-14-hybrid-retrieval-results.md
git commit -m "docs: hybrid retrieval before and after results"
```

---

## Self-review notes

**Spec coverage.** Layer 1 is Task 6. Layer 2 is Tasks 5 and 7. Layer 3 is Tasks 1, 3, 4 and 7. Placement is Tasks 7 and 8. Local environment is Tasks 1, 2 and 10. Failure behaviour is Tasks 7 and 9. Measurement is Task 11.

**Three things the design document did not know, all now tasks.**

1. `session_chunks` and `match_session_chunks` are defined nowhere in the repository. Task 1 captures them before extending them.
2. `retrieve_sp_chunks` asks for `match_count` rows across every document type and then filters to Special Provision in Python, so a check asking for 8 passages routinely receives 3. Task 4 fixes it, and it may be a larger real-world win than either new layer.
3. The shared ranker already deduplicates continuation chunks sharing a `section_id`. Once Task 6 gives Special Provision chunks that field, that behaviour would strip exactly the continuation chunks a multi-chunk table needs, so Task 7 keeps pinned rows outside fusion.

**Ordering.** Task 1 gates everything that touches the database. Task 3 must precede Task 7. Task 5 must precede Task 7. Task 6 must precede Task 10. Task 7 must precede Tasks 8 and 9.

**Type consistency.** `Anchors` and `extract_anchors` are defined in Task 5 and used in Tasks 7 and 11. `fuse` is defined in Task 3 and used in Task 7. `RetrievalResult` and `retrieve_for_check` are defined in Task 7 and used in Tasks 8, 9 and 10. `retrieve_sp_chunks`'s new `match_threshold` and `doc_type` keywords are added in Task 4 and used in Task 7. `keyword_search_session_chunks` is created in Task 1 and called in Tasks 7 and 10.

**One deliberate loose end.** Task 9 leaves the mechanism for carrying `anchor_missing` back to the engine open between two options, because the right answer depends on what Task 8 settles on for the closure signature. The task says to pick one and record it in the module docstring rather than leaving both half-built.
