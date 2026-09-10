# Review Citations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every compliance-review check 0–N citation pills that open the real source PDF at the actual retrieved page, verified against what was really retrieved so a hallucinated citation shows up flagged instead of silently linking to a wrong page.

**Architecture:** Search functions for Spec/CSM/Special-Provision/Narrative evidence tag each passage they return (`[cite:sp-0]`, etc.) and hand back a `{tag: EvidenceCandidate}` lookup alongside the tagged text. The LLM copies whichever tags it used into a new `cited_chunk_ids` field. After the (possibly retried) verdict is final, each tag is resolved against the lookup — a match becomes a verified `ReviewCitation` with page/section overwritten from ground truth; a miss becomes an unverified, non-clickable one. Key Map/Estimate checks get an automatic, always-verified page-1 citation (no per-passage retrieval exists for them). The frontend renders these as clickable/flagged pills reusing the existing `PDFViewerModal`.

**Tech Stack:** FastAPI + Pydantic v2 (backend), Next.js/React + TypeScript (frontend), pytest, existing `PDFViewerModal`/`SourcePill` patterns from `SessionChat.tsx`.

## Global Constraints

- Spec/CSM and Special Provision/Narrative citations must be **verified** (matched against a real retrieved passage) before rendering as clickable; an unmatched LLM-claimed citation renders flagged and non-clickable, never dropped and never clickable.
- Key Map/Estimate citations are always a whole-document, page-1 reference (no passage-level retrieval for these — out of scope per the design spec's non-goals).
- Schedule/utility-plan-sourced evidence gets no citation at all (no PDF exists for either).
- No change to `_judge_grounding`'s contract, the jsPDF report export, or any dropped-vs-flagged behavior — flagged, never dropped, per the approved design.
- Full backend test suite (`pytest`) and `npx tsc --noEmit -p frontend` must stay green after every task.

Spec: [`docs/superpowers/specs/2026-09-10-review-citations-design.md`](../specs/2026-09-10-review-citations-design.md)

---

## Task 1: `ReviewCitation` model + citation fields on `EvaluationSchema`/`ReviewCheckResult`

**Files:**
- Modify: `backend/app/models.py`
- Test: `backend/tests/test_models_review_citation.py` (new)

**Interfaces:**
- Produces: `ReviewCitation(kind: Literal["public","private"], doc_type: str, label: str, page_pdf: Optional[int] = None, section_id: Optional[str] = None, verified: bool)`; `EvaluationSchema.cited_chunk_ids: List[str] = []`; `ReviewCheckResult.citations: List[ReviewCitation] = []`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_models_review_citation.py`:

```python
"""backend/tests/test_models_review_citation.py

Tests for ReviewCitation and the new citation-carrying fields on
EvaluationSchema/ReviewCheckResult -- see
docs/superpowers/specs/2026-09-10-review-citations-design.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.models import EvaluationSchema, ReviewCheckResult, ReviewCitation  # noqa: E402


def test_evaluation_schema_defaults_cited_chunk_ids_to_empty_list():
    result = EvaluationSchema(status="Pass", evidence="e", source="s")
    assert result.cited_chunk_ids == []


def test_review_check_result_defaults_citations_to_empty_list():
    result = ReviewCheckResult(
        id="c1", category="Cat", name="Name", status="Pass", evidence="e", source="s",
    )
    assert result.citations == []


def test_review_citation_verified_chunk_shape():
    citation = ReviewCitation(
        kind="private", doc_type="special_provision", label="Special Provision",
        page_pdf=5, section_id="sp_2_1", verified=True,
    )
    assert citation.kind == "private"
    assert citation.page_pdf == 5
    assert citation.verified is True


def test_review_citation_unverified_has_no_page():
    citation = ReviewCitation(
        kind="private", doc_type="unknown", label="Unverified citation (sp-9)", verified=False,
    )
    assert citation.page_pdf is None
    assert citation.section_id is None
    assert citation.verified is False


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/../.venv/Scripts/python.exe -m pytest tests/test_models_review_citation.py -v` (from repo root: `& "C:\Users\karan\OneDrive\Desktop\njdot-chatbot\.venv\Scripts\python.exe" -m pytest backend/tests/test_models_review_citation.py -v`)

Expected: FAIL with `ImportError: cannot import name 'ReviewCitation' from 'app.models'`.

- [ ] **Step 3: Add `ReviewCitation` and the two new fields**

In `backend/app/models.py`, add `Literal` is already imported. Insert a new class right before `class ReviewCheckResult(BaseModel):` (currently at line 120):

```python
class ReviewCitation(BaseModel):
    """One source reference attached to a review check's evidence.

    ``verified=True`` means either the LLM named a tagged passage that was
    actually retrieved for this check (chunk-level citation, Spec/CSM/SP/
    Narrative), or it's an automatic whole-document reference (Key Map/
    Estimate, which have no per-passage retrieval to verify against).
    ``verified=False`` means the LLM claimed a citation that couldn't be
    matched to anything retrieved -- shown flagged, never clickable.
    """

    kind:       Literal["public", "private"]
    # public  -> doc_type is the doc_name for GET /api/pdf/{doc_type}
    # private -> doc_type is the doc_type for GET /api/review/{project_id}/pdf/{doc_type}
    doc_type:   str
    label:      str
    page_pdf:   Optional[int] = None
    section_id: Optional[str] = None
    verified:   bool
```

Then modify `EvaluationSchema` (currently lines 102-108) to add one field:

```python
class EvaluationSchema(BaseModel):
    """Structured output shape enforced via ``.with_structured_output()`` for
    each individual compliance-check LLM call."""

    status:   Literal["Pass", "Fail", "Missing"]
    evidence: str   # verbatim extraction or exact metric found
    source:   str   # page number, document name, or Task ID
    cited_chunk_ids: List[str] = []   # tags copied verbatim from tagged evidence passages
```

Then modify `ReviewCheckResult` to add one field:

```python
class ReviewCheckResult(BaseModel):
    """One evaluated check, with catalog identity attached to its EvaluationSchema result."""

    id:       str
    category: str
    name:     str
    status:   Literal["Pass", "Fail", "Missing"]
    evidence: str
    source:   str
    citations: List[ReviewCitation] = []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `& "C:\Users\karan\OneDrive\Desktop\njdot-chatbot\.venv\Scripts\python.exe" -m pytest backend/tests/test_models_review_citation.py -v`

Expected: PASS (4 passed).

- [ ] **Step 5: Run full backend suite to confirm no regression**

Run: `& "C:\Users\karan\OneDrive\Desktop\njdot-chatbot\.venv\Scripts\python.exe" -m pytest backend/tests -q`

Expected: all tests pass (adding fields with defaults doesn't change any existing model's serialized shape observed by other tests).

- [ ] **Step 6: Commit**

```bash
git add backend/app/models.py backend/tests/test_models_review_citation.py
git commit -m "$(cat <<'EOF'
feat: add ReviewCitation model and citation fields to review schemas

ReviewCitation carries the doc/page/verified shape a review-check
citation needs; EvaluationSchema.cited_chunk_ids lets the LLM name which
tagged evidence passage it used, ReviewCheckResult.citations carries the
resolved result. No behavior change yet -- nothing populates these until
the eval_engine/review.py wiring in later tasks.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `EvidenceCandidate`/`CitedSearch` types + tagged `build_narrative_text`

**Files:**
- Modify: `backend/app/compliance/eval_engine.py`
- Test: `backend/tests/test_eval_engine.py` (append)

**Interfaces:**
- Consumes: none new (uses `Neo4jGraph.query`, unchanged).
- Produces: `EvidenceCandidate(kind, doc_type, label, page_pdf=None, section_id=None)` (plain `@dataclass`, module-level in `eval_engine.py`); `CitedSearch = Callable[[str], Tuple[str, Dict[str, EvidenceCandidate]]]`; `build_narrative_text(graph, project_id="default") -> Tuple[str, Dict[str, EvidenceCandidate]]` (return type changed from `str`).

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_eval_engine.py`, update the import block (currently lines 25-34) to also pull in `EvidenceCandidate` and `build_narrative_text`:

```python
from app.compliance.eval_engine import (  # noqa: E402
    EvidenceCandidate,
    _DeterministicContext,
    _accumulate_usage,
    _evaluate_one_check,
    _judge_grounding,
    _retry_with_correction,
    build_narrative_text,
    evaluate_checks,
)
```

Then add these two tests anywhere after `_fake_checks` (e.g. right after it):

```python
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
    assert candidates["narrative-1"].label == "n2"
    assert candidates["narrative-1"].page_pdf == 4


def test_build_narrative_text_empty_when_no_chunks():
    graph = MagicMock()
    graph.query.return_value = []

    text, candidates = build_narrative_text(graph)

    assert text == "DESIGNER'S NARRATIVE: not provided."
    assert candidates == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `& "C:\Users\karan\OneDrive\Desktop\njdot-chatbot\.venv\Scripts\python.exe" -m pytest backend/tests/test_eval_engine.py -v -k narrative_text`

Expected: FAIL (`ImportError: cannot import name 'EvidenceCandidate'`, or once that's fixed, a tuple-unpack/assertion failure since `build_narrative_text` still returns a plain string).

- [ ] **Step 3: Add the types and retag `build_narrative_text`**

In `backend/app/compliance/eval_engine.py`, change the typing import (currently line 44):

```python
from typing import Callable, Dict, List, Literal, Optional, Tuple
```

Right after `logger = logging.getLogger(__name__)` (currently line 59), before `_MAX_FACT_ROWS = 40`, add:

```python
@dataclass
class EvidenceCandidate:
    """One retrieved passage's citation metadata, keyed by the inline tag
    (e.g. ``"sp-0"``) a search function embeds in its returned evidence text
    -- lets ``_evaluate_one_check`` verify which passage (if any) the LLM's
    ``cited_chunk_ids`` actually refers to, mirroring how
    ``CitationSerializer`` validates chat citations against real retrieved
    chunks (see docs/superpowers/specs/2026-09-10-review-citations-design.md).
    """

    kind:       Literal["public", "private"]
    doc_type:   str
    label:      str
    page_pdf:   Optional[int] = None
    section_id: Optional[str] = None


# A citable search function returns (tagged_evidence_text, {tag: candidate})
# instead of a plain string, so the tag(s) the LLM copies into
# EvaluationSchema.cited_chunk_ids can be resolved back to real metadata.
CitedSearch = Callable[[str], Tuple[str, Dict[str, EvidenceCandidate]]]
```

Then replace `build_narrative_text` (currently lines 247-263):

```python
def build_narrative_text(
    graph: Neo4jGraph, project_id: str = "default",
) -> Tuple[str, Dict[str, EvidenceCandidate]]:
    """Full designer-narrative text, concatenated in chunk order and tagged
    per-chunk for citation verification (e.g. ``[cite:narrative-0]``).

    The narrative is small (~10 pages) — cheaper and more reliable to include
    it whole (shared prefix, still cacheable across every check that
    requests ``"narrative"``) than to build per-check retrieval for it.
    """
    rows = graph.query(
        "MATCH (c:NarrativeChunk {projectId: $pid}) "
        "RETURN c.id AS id, c.heading AS heading, c.text AS text, c.pagePdf AS pagePdf "
        "ORDER BY c.id",
        params={"pid": project_id},
    )
    if not rows:
        return "DESIGNER'S NARRATIVE: not provided.", {}
    parts: List[str] = []
    candidates: Dict[str, EvidenceCandidate] = {}
    for i, r in enumerate(rows):
        tag = f"narrative-{i}"
        heading = r["heading"] or r["id"]
        parts.append(f"[cite:{tag}] [{heading}]\n{r['text']}")
        candidates[tag] = EvidenceCandidate(
            kind="private", doc_type="narrative", label=heading, page_pdf=r.get("pagePdf"),
        )
    return "DESIGNER'S NARRATIVE:\n\n" + "\n\n".join(parts), candidates
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `& "C:\Users\karan\OneDrive\Desktop\njdot-chatbot\.venv\Scripts\python.exe" -m pytest backend/tests/test_eval_engine.py -v`

Expected: all pass, including the two new ones. (The two `evaluate_checks` progress tests still pass unchanged here — they patch `build_narrative_text` to return `""` directly, so the real function's new return shape doesn't affect them yet; `evaluate_checks`'s own call site is only updated in Task 4.)

- [ ] **Step 5: Run full backend suite**

Run: `& "C:\Users\karan\OneDrive\Desktop\njdot-chatbot\.venv\Scripts\python.exe" -m pytest backend/tests -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/compliance/eval_engine.py backend/tests/test_eval_engine.py
git commit -m "$(cat <<'EOF'
feat: tag narrative evidence with per-chunk citation candidates

Adds EvidenceCandidate/CitedSearch (the shared contract every citable
search function will return) and retags build_narrative_text's output
per NarrativeChunk, threading through pagePdf (already stored in Neo4j
but never selected before). evaluate_checks's own call site still expects
a plain string from build_narrative_text -- updated in a later task once
_evaluate_one_check is ready to consume the tuple.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Tag SP and static-doc (Spec/CSM) search functions

**Files:**
- Modify: `backend/app/api/review.py`
- Test: `backend/tests/test_review_search_fns.py` (new)

**Interfaces:**
- Consumes: `EvidenceCandidate`, `CitedSearch` from `app.compliance.eval_engine` (Task 2).
- Produces: `_build_sp_search_fn(...) -> Optional[CitedSearch]`, `_build_sp_search_fn_from_supabase(...) -> Optional[CitedSearch]`, `_build_static_doc_search_fn(...) -> Optional[CitedSearch]` — all now return `(tagged_text, {tag: EvidenceCandidate})` instead of `str`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_review_search_fns.py`:

```python
"""backend/tests/test_review_search_fns.py

Tests for the review-citations feature's evidence-tagging search functions
in app.api.review: _build_sp_search_fn, _build_sp_search_fn_from_supabase,
and _build_static_doc_search_fn now return (tagged_text, {tag: candidate})
instead of a plain string, so _evaluate_one_check can verify which passage
(if any) the LLM's cited_chunk_ids actually refers to. See
docs/superpowers/specs/2026-09-10-review-citations-design.md.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.api.review import (  # noqa: E402
    _build_sp_search_fn,
    _build_sp_search_fn_from_supabase,
    _build_static_doc_search_fn,
)
from app.compliance.eval_engine import EvidenceCandidate  # noqa: E402


def test_build_sp_search_fn_tags_top_result_with_page_pdf():
    sp_chunks = [
        {"content": "Gas work prohibited in July.", "metadata": {"page_pdf": 7}},
        {"content": "Unrelated clause about bonds.", "metadata": {"page_pdf": 2}},
    ]
    sp_vectors = [[1.0, 0.0], [0.0, 1.0]]
    embeddings = MagicMock()
    embeddings.embed_query.return_value = [1.0, 0.0]  # matches first chunk exactly

    search_fn = _build_sp_search_fn(sp_chunks, sp_vectors, embeddings)
    text, candidates = search_fn("gas work restriction", top_k=1)

    assert "[cite:sp-0]" in text
    assert "Gas work prohibited in July." in text
    assert candidates["sp-0"] == EvidenceCandidate(
        kind="private", doc_type="special_provision", label="Special Provision", page_pdf=7,
    )


def test_build_sp_search_fn_returns_none_for_no_chunks():
    assert _build_sp_search_fn([], [], MagicMock()) is None


def test_build_sp_search_fn_from_supabase_tags_rows_with_page_pdf():
    db = MagicMock()
    db.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.count = 1

    with patch("app.api.review.retrieve_sp_chunks", return_value=[
        {"content": "Multi-year funding clause text.", "metadata": {"page_pdf": 12}},
    ]):
        search_fn = _build_sp_search_fn_from_supabase(db, MagicMock(), "proj1")
        text, candidates = search_fn("funding")

    assert "[cite:sp-0]" in text
    assert candidates["sp-0"].page_pdf == 12
    assert candidates["sp-0"].kind == "private"


def test_build_static_doc_search_fn_tags_results_with_doc_and_page():
    fake_searcher = MagicMock()
    fake_searcher.search.return_value = [
        {"content": "Proposal bond must be 10% of bid.", "metadata": {
            "doc": "Spec2019", "section_id": "102.03", "section_title": "Proposal Guaranty", "page_pdf": 45,
        }},
    ]
    with patch("app.api.review.VectorSearcher", return_value=fake_searcher):
        search_fn = _build_static_doc_search_fn("specs_2019")
        text, candidates = search_fn("proposal bond requirements")

    tag = "specs_2019-0"
    assert f"[cite:{tag}]" in text
    assert candidates[tag] == EvidenceCandidate(
        kind="public", doc_type="Spec2019", label="Proposal Guaranty",
        page_pdf=45, section_id="102.03",
    )


def test_build_static_doc_search_fn_returns_none_when_searcher_init_fails():
    with patch("app.api.review.VectorSearcher", side_effect=RuntimeError("no key")):
        assert _build_static_doc_search_fn("specs_2019") is None


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `& "C:\Users\karan\OneDrive\Desktop\njdot-chatbot\.venv\Scripts\python.exe" -m pytest backend/tests/test_review_search_fns.py -v`

Expected: FAIL (`text, candidates = search_fn(...)` raises `ValueError: too many values to unpack` or similar, since these functions still return a plain string).

- [ ] **Step 3: Retag the three search functions**

In `backend/app/api/review.py`, change the typing import (currently line 47):

```python
from typing import Any, Callable, Dict, List, Optional, Tuple
```

Add `EvidenceCandidate`/`CitedSearch` to the existing eval_engine import (currently line 60):

```python
from app.compliance.eval_engine import CitedSearch, EvidenceCandidate, evaluate_checks
```

Replace `_build_sp_search_fn` (currently lines 337-360):

```python
def _build_sp_search_fn(
    sp_chunks: List[Dict[str, Any]], sp_vectors: List[List[float]], embeddings: OpenAIEmbeddings,
) -> Optional[CitedSearch]:
    """In-process cosine-ranked Special Provision search over already-chunked
    and already-embedded SP text, tagging each returned passage for citation
    verification (e.g. ``[cite:sp-0]``).

    Takes precomputed ``sp_chunks``/``sp_vectors`` rather than raw bytes so
    the caller (``_run_review_pipeline``) can chunk/embed the SP PDF exactly
    once and share the result with both this search closure and the
    ``insert_session_chunks`` call that persists them to Supabase
    ``session_chunks`` — previously each computed its own chunks/embeddings
    independently, doubling SP embedding calls on every fresh review.
    """
    if not sp_chunks:
        return None

    def _search(query: str, top_k: int = 5) -> Tuple[str, Dict[str, EvidenceCandidate]]:
        q_vec = embeddings.embed_query(query)
        scored = sorted(zip(sp_chunks, sp_vectors), key=lambda cv: -_cosine(q_vec, cv[1]))
        top = [c for c, _ in scored[:top_k]]
        if not top:
            return "No matching Special Provision text found.", {}
        parts: List[str] = []
        candidates: Dict[str, EvidenceCandidate] = {}
        for i, chunk in enumerate(top):
            tag = f"sp-{i}"
            parts.append(f"[cite:{tag}] {chunk['content']}")
            candidates[tag] = EvidenceCandidate(
                kind="private", doc_type="special_provision", label="Special Provision",
                page_pdf=(chunk.get("metadata") or {}).get("page_pdf"),
            )
        return "\n\n---\n\n".join(parts), candidates

    return _search
```

Replace `_build_sp_search_fn_from_supabase` (currently lines 363-387):

```python
def _build_sp_search_fn_from_supabase(
    db: Any, embeddings: OpenAIEmbeddings, project_id: str,
) -> Optional[CitedSearch]:
    """Special Provision search backed by ``session_chunks`` -- the
    ``reseed=False`` fast path's equivalent of ``_build_sp_search_fn``,
    without re-parsing, re-chunking, or re-embedding the PDF. Reuses the same
    ``retrieve_sp_chunks`` chat already calls, so review and chat can never
    disagree about how SP retrieval works. Returns ``None`` if this project
    has no SP chunks (matches "no SP uploaded" behavior).
    """
    existing = (
        db.table("session_chunks").select("id", count="exact")
        .eq("session_id", project_id).eq("doc_type", "special_provision")
        .limit(1).execute()
    )
    if not existing.count:
        return None

    def _search(query: str) -> Tuple[str, Dict[str, EvidenceCandidate]]:
        rows = retrieve_sp_chunks(db, embeddings.embed_query, project_id, query)
        if not rows:
            return "No matching Special Provision text found.", {}
        parts: List[str] = []
        candidates: Dict[str, EvidenceCandidate] = {}
        for i, r in enumerate(rows):
            tag = f"sp-{i}"
            parts.append(f"[cite:{tag}] {r['content']}")
            candidates[tag] = EvidenceCandidate(
                kind="private", doc_type="special_provision", label="Special Provision",
                page_pdf=(r.get("metadata") or {}).get("page_pdf"),
            )
        return "\n\n---\n\n".join(parts), candidates

    return _search
```

Replace `_build_static_doc_search_fn` (currently lines 579-605):

```python
def _build_static_doc_search_fn(collection: str, match_count: int = 5) -> Optional[CitedSearch]:
    """Search-function factory for a static, pre-ingested reference
    collection (Standard Specifications or the Construction Scheduling
    Manual), tagging each returned passage for citation verification (e.g.
    ``[cite:specs_2019-0]``). Unlike ``_build_sp_search_fn*``, this doesn't
    depend on any per-review upload — the collection is embedded once,
    system-wide, by ``backend/scripts/ingest_specs.py`` — so it's built
    unconditionally for every review. Returns ``None`` only if the searcher
    itself can't be constructed (e.g. missing OpenAI key), so a check
    requesting this source degrades to "Missing" instead of crashing the
    whole review.
    """
    try:
        searcher = VectorSearcher()
    except Exception:
        logger.exception("Failed to initialize VectorSearcher for collection=%s", collection)
        return None

    def _search(query: str) -> Tuple[str, Dict[str, EvidenceCandidate]]:
        try:
            results = searcher.search(query, collection=collection, match_count=match_count)
        except Exception:
            logger.exception("Static-doc search failed for collection=%s", collection)
            return "No matching reference text found (search error).", {}
        if not results:
            return "No matching reference text found.", {}
        parts: List[str] = []
        candidates: Dict[str, EvidenceCandidate] = {}
        for i, r in enumerate(results):
            tag = f"{collection}-{i}"
            meta = r.get("metadata") or {}
            parts.append(f"[cite:{tag}] {r['content']}")
            candidates[tag] = EvidenceCandidate(
                kind="public", doc_type=meta.get("doc", collection),
                label=meta.get("section_title") or meta.get("doc") or collection,
                page_pdf=meta.get("page_pdf"), section_id=meta.get("section_id"),
            )
        return "\n\n---\n\n".join(parts), candidates

    return _search
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `& "C:\Users\karan\OneDrive\Desktop\njdot-chatbot\.venv\Scripts\python.exe" -m pytest backend/tests/test_review_search_fns.py -v`

Expected: all 5 pass.

- [ ] **Step 5: Run full backend suite**

Run: `& "C:\Users\karan\OneDrive\Desktop\njdot-chatbot\.venv\Scripts\python.exe" -m pytest backend/tests -q`

Expected: all pass. (`test_review_endpoint.py:804` and `test_review_rerun_endpoint.py:287,293` patch `_build_static_doc_search_fn`/`_build_sp_search_fn_from_supabase` to return `None` directly — unaffected by the non-`None` return shape change.)

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/review.py backend/tests/test_review_search_fns.py
git commit -m "$(cat <<'EOF'
feat: tag SP/Spec/CSM search results with citation candidates

_build_sp_search_fn, _build_sp_search_fn_from_supabase, and
_build_static_doc_search_fn now return (tagged_text, {tag: candidate})
instead of a plain joined string, preserving the page_pdf/section_id
metadata each already retrieves but previously discarded before it
reached the check evaluator. eval_engine's call site still expects a
plain string -- updated in the next task.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Build citations in `_evaluate_one_check`, wire `evaluate_checks`

**Files:**
- Modify: `backend/app/compliance/eval_engine.py`
- Test: `backend/tests/test_eval_engine.py` (modify existing + append)

**Interfaces:**
- Consumes: `EvidenceCandidate`, `CitedSearch` (Task 2); `ReviewCitation` (Task 1); the retagged search functions (Task 3, via `evaluate_checks`'s existing parameters).
- Produces: `_evaluate_one_check(..., narrative_result: Tuple[str, Dict[str, EvidenceCandidate]], sp_search_fn: Optional[CitedSearch], spec_search_fn: Optional[CitedSearch], csm_search_fn: Optional[CitedSearch], ...) -> Tuple[ReviewCheckResult, Dict[str, int]]` (renamed/retyped params; `ReviewCheckResult.citations` now populated); `evaluate_checks(...)` unchanged public signature except `sp_search_fn`/`spec_search_fn`/`csm_search_fn` retyped to `Optional[CitedSearch]`.

- [ ] **Step 1: Update the existing test helper and progress-test patches (will fail until Step 3)**

In `backend/tests/test_eval_engine.py`:

1. Update the import block to add `EvidenceCandidate` (if not already added in Task 2) and `ReviewCitation`:

```python
from app.compliance.eval_engine import (  # noqa: E402
    EvidenceCandidate,
    _DeterministicContext,
    _accumulate_usage,
    _evaluate_one_check,
    _judge_grounding,
    _retry_with_correction,
    build_narrative_text,
    evaluate_checks,
)
from app.config import config  # noqa: E402
from app.models import EvaluationSchema, GroundingJudgment, ReviewCheckResult, ReviewCitation  # noqa: E402
```

2. In both `test_evaluate_checks_reports_progress_via_on_progress_callback` and `test_evaluate_checks_works_without_on_progress`, change the `build_narrative_text` patch from:

```python
         patch("app.compliance.eval_engine.build_narrative_text", return_value=""), \
```

to:

```python
         patch("app.compliance.eval_engine.build_narrative_text", return_value=("", {})), \
```

3. In `_call_evaluate_one_check` (currently lines 198-214), rename the `narrative_text` kwarg to `narrative_result` with the new tuple shape:

```python
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
```

- [ ] **Step 2: Add the new citation-building tests**

Append these tests to `backend/tests/test_eval_engine.py`:

```python
def test_evaluate_one_check_builds_verified_citation_from_matched_tag():
    check = _make_check(source_files=["sp"])
    original = EvaluationSchema(
        status="Fail", evidence="SP section bars gas work in July.", source="SP 105.03",
        cited_chunk_ids=["sp-0"],
    )
    llm = _FakeStructuredLLM([(original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}})])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=True, reason="fine"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    def sp_search_fn(query):
        return "[cite:sp-0] Gas work is prohibited in July.", {
            "sp-0": EvidenceCandidate(kind="private", doc_type="special_provision", label="Special Provision", page_pdf=7),
        }

    result, _ = _call_evaluate_one_check(check, llm, judge, sp_search_fn=sp_search_fn)

    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.verified is True
    assert citation.page_pdf == 7
    assert citation.doc_type == "special_provision"


def test_evaluate_one_check_flags_unmatched_citation_tag():
    check = _make_check(source_files=["sp"])
    original = EvaluationSchema(
        status="Fail", evidence="claims to quote SP text", source="SP 105.03",
        cited_chunk_ids=["sp-99"],
    )
    llm = _FakeStructuredLLM([(original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}})])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=True, reason="fine"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    def sp_search_fn(query):
        return "[cite:sp-0] Gas work is prohibited in July.", {
            "sp-0": EvidenceCandidate(kind="private", doc_type="special_provision", label="Special Provision", page_pdf=7),
        }

    result, _ = _call_evaluate_one_check(check, llm, judge, sp_search_fn=sp_search_fn)

    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.verified is False
    assert citation.page_pdf is None


def test_evaluate_one_check_adds_automatic_keymap_and_estimate_citations():
    check = _make_check(source_files=["keymap", "estimate", "schedule"])
    original = EvaluationSchema(status="Pass", evidence="utility crosses I-195", source="key map")
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


def test_evaluate_one_check_downgraded_missing_keeps_automatic_citations_only():
    check = _make_check(source_files=["sp", "keymap"])
    original = EvaluationSchema(
        status="Fail", evidence="wrong reading", source="schedule", cited_chunk_ids=["sp-0"],
    )
    retried = EvaluationSchema(status="Fail", evidence="still wrong", source="schedule")
    llm = _FakeStructuredLLM([
        (original, {"input_tokens": 10, "output_tokens": 5, "input_token_details": {}}),
        (retried, {"input_tokens": 12, "output_tokens": 6, "input_token_details": {}}),
    ])
    judge = _FakeStructuredLLM([
        (GroundingJudgment(grounded=False, reason="dates show compliance"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
        (GroundingJudgment(grounded=False, reason="still contradicts dates"), {"input_tokens": 20, "output_tokens": 5, "input_token_details": {}}),
    ])

    def sp_search_fn(query):
        return "[cite:sp-0] Some SP text.", {
            "sp-0": EvidenceCandidate(kind="private", doc_type="special_provision", label="Special Provision", page_pdf=1),
        }

    result, _ = _call_evaluate_one_check(
        check, llm, judge, sp_search_fn=sp_search_fn, keymap_facts="KEY MAP FACTS: ...",
    )

    assert result.status == "Missing"
    assert len(result.citations) == 1
    assert result.citations[0].doc_type == "key_map"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `& "C:\Users\karan\OneDrive\Desktop\njdot-chatbot\.venv\Scripts\python.exe" -m pytest backend/tests/test_eval_engine.py -v`

Expected: FAIL — `_call_evaluate_one_check` raises `TypeError: _evaluate_one_check() got an unexpected keyword argument 'narrative_result'` (the function still takes `narrative_text`), and the new citation tests fail on `result.citations == []`.

- [ ] **Step 4: Implement citation building in `_evaluate_one_check` and wire `evaluate_checks`**

In `backend/app/compliance/eval_engine.py`, add `ReviewCitation` to the models import (currently line 56):

```python
from app.models import EvaluationSchema, GroundingJudgment, ReviewCheckResult, ReviewCitation
```

Update the system prompt (currently lines 63-79) — add one bullet at the end, right before the closing `"""`:

```python
_STATIC_SYSTEM_PROMPT = """\
You are an expert NJDOT Construction Schedule Compliance Agent evaluating ONE \
compliance check at a time against the evidence provided in the user message \
(precomputed schedule/CPM facts, key map facts, or Special Provision \
excerpts, depending on the check).

Rules:
- Base your answer only on the evidence provided — do not assume facts not shown.
- status "Pass": the evidence clearly satisfies the rule.
- status "Fail": the evidence clearly violates the rule.
- status "Missing": the evidence needed to evaluate the rule is not present in \
what was provided.
- evidence: quote the specific fact(s) used (activity IDs, dates, SP section \
number, narrative text) — keep it concise.
- source: cite where the evidence came from (e.g. an activity ID, an SP \
section number, "narrative", or "no data provided").
- Some passages above are tagged like "[cite:sp-0]". If your evidence draws \
on a tagged passage, copy its tag id (the part after "cite:", e.g. "sp-0" — \
no brackets, no "cite:" prefix) into cited_chunk_ids. Passages without a tag \
(schedule facts, key map facts, estimate facts) don't need one.
"""
```

Replace `_evaluate_one_check`'s signature (currently lines 439-455) and its evidence-building block (currently lines 514-535), and add the citation-resolution block right before the final `return` (currently lines 613-616). The full function becomes:

```python
def _evaluate_one_check(
    check: CheckDef,
    structured_llm: Runnable,
    structured_judge_llm: Runnable,
    schedule_facts: str,
    narrative_result: Tuple[str, Dict[str, EvidenceCandidate]],
    sp_search_fn: Optional[CitedSearch],
    spec_search_fn: Optional[CitedSearch],
    csm_search_fn: Optional[CitedSearch],
    keymap_facts: Optional[str],
    estimate_facts: Optional[str],
    utility_plan_search_fn: Optional[Callable[[str], str]],
    deterministic: "_DeterministicContext",
    project_id: str,
    user_id: Optional[str],
    langfuse_handler,
) -> Tuple[ReviewCheckResult, Dict[str, int]]:
    """Evaluate a single check and return its result plus the token usage of
    all the LLM calls this check made (original answer, plus the grounding
    judge and any retry/re-judge). Touches no shared state — safe to run
    concurrently in a worker thread (see ``evaluate_checks``'s
    ``ThreadPoolExecutor``).
    """
    usage_totals = {
        "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "llm_call_count": 0,
        "judged": 0, "ungrounded": 0, "downgraded": 0,
    }
    sources = check.source_files or ["schedule"]

    missing_sources = []
    if "sp" in sources and sp_search_fn is None:
        missing_sources.append("Special Provision (not uploaded for this review)")
    if "keymap" in sources and keymap_facts is None:
        missing_sources.append("Key Map (not uploaded for this review)")
    if "estimate" in sources and estimate_facts is None:
        missing_sources.append("Cost Estimate (not uploaded for this review)")
    if "spec" in sources and spec_search_fn is None:
        missing_sources.append("Standard Specifications")
    if "csm" in sources and csm_search_fn is None:
        missing_sources.append("Construction Scheduling Manual")
    if missing_sources:
        return ReviewCheckResult(
            id=check.check_key, category=check.category, name=check.name,
            status="Missing", evidence=f"Not available for this review: {', '.join(missing_sources)}.",
            source="no data provided",
        ), usage_totals

    deterministic_fn = _DETERMINISTIC_EVALUATORS.get(check.check_type)
    if deterministic_fn is not None:
        return deterministic_fn(check, deterministic), usage_totals

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
    citation_lookup: Dict[str, EvidenceCandidate] = {}
    try:
        evidence_parts: List[str] = []

        def _add(text: str, candidates: Dict[str, EvidenceCandidate]) -> None:
            evidence_parts.append(text)
            citation_lookup.update(candidates)

        if "schedule" in sources:
            evidence_parts.append(schedule_facts)
        if "narrative" in sources:
            _add(*narrative_result)
        if "sp" in sources:
            _add(*sp_search_fn(check.instruction))
        if "keymap" in sources:
            evidence_parts.append(keymap_facts)
        if "estimate" in sources:
            evidence_parts.append(estimate_facts)
        if "spec" in sources:
            _add(*spec_search_fn(check.instruction))
        if "csm" in sources:
            _add(*csm_search_fn(check.instruction))
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
                    result = retried
                elif retried is not None and re_judgment is not None and re_judgment.grounded:
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

    citations: List[ReviewCitation] = []
    for tag in result.cited_chunk_ids:
        candidate = citation_lookup.get(tag)
        if candidate is not None:
            citations.append(ReviewCitation(
                kind=candidate.kind, doc_type=candidate.doc_type, label=candidate.label,
                page_pdf=candidate.page_pdf, section_id=candidate.section_id, verified=True,
            ))
        else:
            citations.append(ReviewCitation(
                kind="private", doc_type="unknown", label=f"Unverified citation ({tag})",
                verified=False,
            ))
    if "keymap" in sources and keymap_facts is not None:
        citations.append(ReviewCitation(
            kind="private", doc_type="key_map", label="Key Map", page_pdf=1, verified=True,
        ))
    if "estimate" in sources and estimate_facts is not None:
        citations.append(ReviewCitation(
            kind="private", doc_type="estimate", label="Estimate", page_pdf=1, verified=True,
        ))

    return ReviewCheckResult(
        id=check.check_key, category=check.category, name=check.name,
        status=result.status, evidence=result.evidence, source=result.source,
        citations=citations,
    ), usage_totals
```

(Only the signature, the evidence-building block inside the `try:`, and the new citation block before the final `return` are new/changed — the judge/retry orchestration in the middle is reproduced above verbatim, unchanged, to show the complete function.)

Now update `evaluate_checks`'s signature (currently lines 619-635) — retype the three search-fn parameters:

```python
def evaluate_checks(
    checks: List[CheckDef],
    graph: Neo4jGraph,
    llm: BaseChatModel,
    sp_search_fn: Optional[CitedSearch] = None,
    spec_search_fn: Optional[CitedSearch] = None,
    csm_search_fn: Optional[CitedSearch] = None,
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

And its body (currently lines 689-733) — rename `narrative_text` to `narrative_result` at both the computation site and the `executor.submit(...)` call:

```python
    schedule_facts = "\n\n".join([
        build_compliance_facts(graph, project_id),
        build_milestones(graph, project_id),
        build_activity_roster(graph, project_id),
    ])
    narrative_result = build_narrative_text(graph, project_id)
```

```python
            future_to_index = {
                executor.submit(
                    _evaluate_one_check, check, structured_llm, structured_judge_llm,
                    schedule_facts, narrative_result,
                    sp_search_fn, spec_search_fn, csm_search_fn, keymap_facts, estimate_facts,
                    utility_plan_search_fn,
                    deterministic_ctx, project_id, user_id, langfuse_handler,
                ): i
                for i, check in enumerate(checks)
            }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `& "C:\Users\karan\OneDrive\Desktop\njdot-chatbot\.venv\Scripts\python.exe" -m pytest backend/tests/test_eval_engine.py -v`

Expected: all pass (existing + 4 new citation tests).

- [ ] **Step 6: Run full backend suite**

Run: `& "C:\Users\karan\OneDrive\Desktop\njdot-chatbot\.venv\Scripts\python.exe" -m pytest backend/tests -q`

Expected: all pass. `test_review_endpoint.py`/`test_review_rerun_endpoint.py` patch `evaluate_checks` itself (`app.api.review.evaluate_checks`), so its retyped parameters don't affect them.

- [ ] **Step 7: Commit**

```bash
git add backend/app/compliance/eval_engine.py backend/tests/test_eval_engine.py
git commit -m "$(cat <<'EOF'
feat: resolve LLM-claimed citations against retrieved evidence

_evaluate_one_check now builds a citation_lookup from every tagged
search-fn/narrative result it uses, asks the LLM to name which tags its
evidence drew on (cited_chunk_ids), and resolves each tag after the
(possibly retried) verdict is final: a match becomes a verified
ReviewCitation with page/section from ground truth, a miss becomes an
unverified one -- mirroring how CitationSerializer validates chat
citations. Key Map/Estimate checks also get an automatic, always-verified
page-1 citation, since neither has per-passage retrieval to verify
against. evaluate_checks is wired to pass the now-tagged narrative result
and retyped search functions through.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Pass `citations` through `_to_frontend_shape`

**Files:**
- Modify: `backend/app/api/review.py`
- Test: `backend/tests/test_review_frontend_shape.py` (new)

**Interfaces:**
- Consumes: `ReviewCheckResult.citations` (Task 1/4).
- Produces: `_to_frontend_shape(response)["checks"][i]["citations"]` — a list of plain dicts (`kind`, `doc_type`, `label`, `page_pdf`, `section_id`, `verified`).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_review_frontend_shape.py`:

```python
"""backend/tests/test_review_frontend_shape.py

Tests that _to_frontend_shape passes each check's citations through to the
frontend's JSON contract. See
docs/superpowers/specs/2026-09-10-review-citations-design.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.api.review import _to_frontend_shape  # noqa: E402
from app.models import ReviewCheckResult, ReviewCitation, ReviewResponse  # noqa: E402


def test_to_frontend_shape_includes_citations_as_plain_dicts():
    response = ReviewResponse(
        project_name="Route 49", project_duration_days=365,
        summary={"passed": 1, "warnings": 0, "failed": 0, "manual_review": 0},
        checks=[
            ReviewCheckResult(
                id="c1", category="Cat", name="Check 1", status="Pass",
                evidence="e", source="s",
                citations=[
                    ReviewCitation(
                        kind="private", doc_type="special_provision", label="Special Provision",
                        page_pdf=7, verified=True,
                    ),
                ],
            ),
        ],
        model_used="claude-sonnet-5",
        project_id="proj1",
    )

    shaped = _to_frontend_shape(response)

    citations = shaped["checks"][0]["citations"]
    assert citations == [{
        "kind": "private", "doc_type": "special_provision", "label": "Special Provision",
        "page_pdf": 7, "section_id": None, "verified": True,
    }]


def test_to_frontend_shape_empty_citations_list_when_none_attached():
    response = ReviewResponse(
        project_name="Route 49", project_duration_days=365,
        summary={"passed": 1, "warnings": 0, "failed": 0, "manual_review": 0},
        checks=[
            ReviewCheckResult(id="c1", category="Cat", name="Check 1", status="Pass", evidence="e", source="s"),
        ],
        model_used="claude-sonnet-5",
        project_id="proj1",
    )

    shaped = _to_frontend_shape(response)

    assert shaped["checks"][0]["citations"] == []


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& "C:\Users\karan\OneDrive\Desktop\njdot-chatbot\.venv\Scripts\python.exe" -m pytest backend/tests/test_review_frontend_shape.py -v`

Expected: FAIL with `KeyError: 'citations'`.

- [ ] **Step 3: Update `_to_frontend_shape`**

In `backend/app/api/review.py`, modify the per-check dict comprehension inside `_to_frontend_shape` (currently lines 697-707):

```python
        "checks": [
            {
                "id": c.id,
                "category": c.category,
                "name": c.name,
                "reasoning": f"Source: {c.source}",
                "status": _STATUS_MAP.get(c.status, "warning"),
                "finding": c.evidence,
                "evidence": c.evidence,
                "citations": [cit.model_dump() for cit in c.citations],
            }
            for c in response.checks
        ],
```

- [ ] **Step 4: Run test to verify it passes**

Run: `& "C:\Users\karan\OneDrive\Desktop\njdot-chatbot\.venv\Scripts\python.exe" -m pytest backend/tests/test_review_frontend_shape.py -v`

Expected: PASS (2 passed).

- [ ] **Step 5: Run full backend suite**

Run: `& "C:\Users\karan\OneDrive\Desktop\njdot-chatbot\.venv\Scripts\python.exe" -m pytest backend/tests -q`

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/review.py backend/tests/test_review_frontend_shape.py
git commit -m "$(cat <<'EOF'
feat: surface review-check citations in the API response

_to_frontend_shape now includes each check's resolved citations
(kind/doc_type/label/page_pdf/section_id/verified) as plain dicts, so
DocumentReview.tsx can render them without any other backend endpoint
changes.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Frontend citation pills on `CheckCard`

**Files:**
- Modify: `frontend/src/components/review/DocumentReview.tsx`

**Interfaces:**
- Consumes: `checks[i].citations` from the API response (Task 5) — `{kind, doc_type, label, page_pdf?, section_id?, verified}[]`.
- Consumes: the existing `PDFViewerModal` component (`frontend/src/components/PDFViewerModal.tsx`) — unchanged.

- [ ] **Step 1: Add the `PDFViewerModal` import and `ReviewCitationItem` type**

In `frontend/src/components/review/DocumentReview.tsx`, add the import right after the existing `API_BASE` import (currently line 4):

```tsx
import { API_BASE } from '@/lib/api'
import PDFViewerModal from '@/components/PDFViewerModal'
```

Add a new interface right before `interface CheckItem` (currently line 15):

```tsx
interface ReviewCitationItem {
  kind:        'public' | 'private'
  doc_type:    string
  label:       string
  page_pdf?:   number | null
  section_id?: string | null
  verified:    boolean
}
```

Add one field to `CheckItem` (currently lines 15-23):

```tsx
interface CheckItem {
  id: string
  category: string
  name: string
  reasoning?: string
  status: 'pass' | 'warning' | 'fail'
  finding: string
  evidence: string
  citations?: ReviewCitationItem[]
}
```

- [ ] **Step 2: Add `CitationPill` and wire it into `CheckCard`**

Replace `CheckCard` (currently lines 179-219) with:

```tsx
function CitationPill({ citation, sessionId, authToken, onOpen }: {
  citation: ReviewCitationItem
  sessionId: string | null
  authToken?: string
  onOpen: (citation: ReviewCitationItem) => void
}) {
  const clickable = citation.verified && citation.page_pdf != null && !!sessionId
  const detail = citation.section_id ? citation.section_id
    : citation.page_pdf != null ? `p.${citation.page_pdf}` : ''

  if (!clickable) {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-semibold text-gray-400"
        title={citation.verified ? undefined : "AI referenced this but it couldn't be verified against retrieved evidence"}
      >
        {citation.verified ? '' : '⚠ '}{citation.label}{detail ? ` · ${detail}` : ''}
      </span>
    )
  }

  return (
    <button
      type="button"
      onClick={() => onOpen(citation)}
      className="inline-flex items-center gap-1 rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-semibold text-blue-700 cursor-pointer hover:opacity-80"
    >
      {citation.label}{detail ? ` · ${detail}` : ''}
    </button>
  )
}

function CheckCard({ check, sessionId, authToken }: {
  check: CheckItem; sessionId: string | null; authToken?: string
}) {
  const [pdfCitation, setPdfCitation] = useState<ReviewCitationItem | null>(null)
  const styles = {
    pass:    { border: 'border-green-500', labelColor: 'text-green-600', label: 'COMPLIANT',    iconBg: 'bg-green-100' },
    fail:    { border: 'border-red-500',   labelColor: 'text-red-600',   label: 'ISSUES FOUND', iconBg: 'bg-red-100' },
    warning: { border: 'border-amber-500', labelColor: 'text-amber-600', label: 'MISSING',      iconBg: 'bg-amber-100' },
  }[check.status]

  return (
    <div className={`flex items-start gap-3.5 rounded-xl border-l-4 ${styles.border} bg-white px-4 py-3.5 shadow-sm ring-1 ring-black/5`}>
      <div className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full ${styles.iconBg}`}>
        {check.status === 'pass' && (
          <svg className="h-3.5 w-3.5 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
          </svg>
        )}
        {check.status === 'warning' && (
          <svg className="h-3.5 w-3.5 text-amber-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
            <path strokeLinecap="round" strokeLinejoin="round"
              d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
          </svg>
        )}
        {check.status === 'fail' && (
          <svg className="h-3.5 w-3.5 text-red-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
        )}
      </div>
      <div className="min-w-0 flex-1">
        <span className={`text-[10px] font-bold uppercase tracking-wider ${styles.labelColor}`}>{styles.label}</span>
        <p className="mt-0.5 text-sm font-semibold text-gray-800">{check.name}</p>
        <p className="mt-1 text-xs text-gray-600 leading-relaxed">{check.finding}</p>
        {check.reasoning && (
          <div className="mt-1.5 rounded bg-gray-50 p-2 text-[11px] text-gray-500 italic border border-gray-100">
            <strong>Reasoning:</strong> {check.reasoning}
          </div>
        )}
        {check.evidence && <p className="mt-1.5 text-[11px] text-gray-400 leading-relaxed italic">{check.evidence}</p>}
        {check.citations && check.citations.length > 0 && (
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {check.citations.map((citation, i) => (
              <CitationPill key={i} citation={citation} sessionId={sessionId} authToken={authToken} onOpen={setPdfCitation} />
            ))}
          </div>
        )}
      </div>
      {pdfCitation && sessionId && (
        <PDFViewerModal
          url={pdfCitation.kind === 'public'
            ? `${API_BASE}/api/pdf/${pdfCitation.doc_type}`
            : `${API_BASE}/api/review/${sessionId}/pdf/${pdfCitation.doc_type}`}
          page={pdfCitation.page_pdf ?? 1}
          authToken={pdfCitation.kind === 'private' ? authToken : undefined}
          requireAuth={pdfCitation.kind === 'private'}
          headerLabel={pdfCitation.label}
          headerDetail={pdfCitation.section_id ?? undefined}
          onClose={() => setPdfCitation(null)}
        />
      )}
    </div>
  )
}
```

- [ ] **Step 3: Thread `sessionId`/`authToken` through `CollapsibleSection` and both `CheckCard` call sites**

Update `CollapsibleSection`'s signature and its one `CheckCard` render (currently lines 221-222 and 250):

```tsx
function CollapsibleSection({ title, checks, expanded, onToggle, sessionId, authToken }: {
  title: string; checks: CheckItem[]; expanded: boolean; onToggle: () => void
  sessionId: string | null; authToken?: string
}) {
```

```tsx
          {checks.map(check => <CheckCard key={check.id} check={check} sessionId={sessionId} authToken={authToken} />)}
```

Update the two call sites inside the main component (currently lines 799-810):

```tsx
                groupSequential(visibleChecks).map((run, i) => (
                  run.header ? (
                    <CollapsibleSection key={i} title={run.header}
                      checks={run.items}
                      expanded={expanded[run.header] ?? true}
                      onToggle={() => toggleSection(run.header!)}
                      sessionId={sessionId} authToken={authToken} />
                  ) : (
                    <div key={i} className="space-y-2.5">
                      {run.items.map(check => <CheckCard key={check.id} check={check} sessionId={sessionId} authToken={authToken} />)}
                    </div>
                  )
                ))
```

- [ ] **Step 4: Type-check**

Run: `cd frontend && npx tsc --noEmit -p .`

Expected: no errors.

- [ ] **Step 5: Manually verify in the browser**

The dev servers are already running (`backend` on :8000, `frontend` on :3000). In the browser:

1. Navigate to the app, start (or open an existing) review project with a Special Provision, Narrative, Key Map, and Estimate all uploaded.
2. Once the checklist renders, confirm:
   - At least one check that used SP/Spec/CSM/Narrative evidence shows a blue clickable pill; clicking it opens `PDFViewerModal` and lands on the page the evidence actually came from (cross-check against the source PDF).
   - Every check whose `source_files` includes `keymap`/`estimate` (and that document was uploaded) shows a "Key Map"/"Estimate" pill that opens at page 1.
   - No check shows a broken link or a pill for a source that wasn't uploaded.
3. If any check happens to produce an unmatched `cited_chunk_ids` tag (harder to force deliberately since it depends on live LLM output), confirm it renders as a greyed, non-clickable pill with the warning glyph and tooltip rather than a link.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/review/DocumentReview.tsx
git commit -m "$(cat <<'EOF'
feat: render citation pills on compliance review checks

CheckCard now shows a clickable pill per verified citation (opening
PDFViewerModal at the real retrieved page, reusing the same public/private
PDF routes and auth pattern SessionChat.tsx already uses) and a flagged,
non-clickable pill for any citation the LLM claimed but that couldn't be
matched against retrieved evidence. Key Map/Estimate checks get an
always-clickable page-1 pill. sessionId/authToken now thread from the
top-level component through CollapsibleSection into CheckCard.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review Notes

**Spec coverage:**
- Data model (`ReviewCitation`, `cited_chunk_ids`, `citations`) — Task 1. ✅
- Evidence tagging for narrative — Task 2. ✅ For SP/Spec/CSM — Task 3. ✅
- Verification/flagging logic in `_evaluate_one_check` — Task 4. ✅
- Automatic Key Map/Estimate page-1 citation — Task 4. ✅
- API passthrough — Task 5. ✅
- Frontend pills + `PDFViewerModal` wiring — Task 6. ✅
- Schedule/utility-plan get no citation — implicitly true throughout (never added to `citation_lookup`, never given an automatic citation) — no task needed, verified by the design's non-goals.

**Known, deliberately out-of-scope gap:** the three deterministic check evaluators (`_evaluate_geo_check`, `_evaluate_cost_gap_check`, `_evaluate_edq_coverage_check`) return before reaching `_evaluate_one_check`'s new citation-building code, so a geo/cost-gap/EDQ-coverage check gets no citation pill even though it's Key Map/Estimate-derived. This matches the committed design spec's own scoping (the automatic citation is described as something `_evaluate_one_check` appends, not the deterministic dispatch path) — flagged here rather than silently expanding scope beyond what was reviewed and approved. If wanted, it's a small, separate follow-up: give `_result()` an optional `citations` parameter and pass the same automatic Key Map/Estimate `ReviewCitation` construction from each deterministic evaluator.

**Type consistency:** `EvidenceCandidate`/`CitedSearch` (Task 2) are used identically in Task 3 (review.py) and Task 4 (eval_engine.py's `_evaluate_one_check`/`evaluate_checks`); `ReviewCitation`'s field names (`kind`, `doc_type`, `label`, `page_pdf`, `section_id`, `verified`) are identical across Task 1 (model), Task 4 (construction), Task 5 (`model_dump()` passthrough), and Task 6 (`ReviewCitationItem` TS interface).
