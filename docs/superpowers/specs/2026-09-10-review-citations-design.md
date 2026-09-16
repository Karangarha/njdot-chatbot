# Review Citations Design

## Background

The compliance review (`POST /api/review`, `backend/app/compliance/eval_engine.py`)
already produces an `evidence`/`source` string per check
(`EvaluationSchema`, `backend/app/models.py:102-108`), but both are
free-text: the LLM names its source as e.g. `"SP § 105.03"` or `"narrative"`
with nothing checking that string against what was actually retrieved. The
chatbot solved exactly this problem for chat answers —
`CitationSerializer.serialize()` (`backend/app/generation/citation_serializer.py`)
validates each LLM-claimed citation against the real chunks retrieved for
that answer, overwrites the page number from ground truth, and flags
`verified=False` on anything that doesn't match a real chunk
(`backend/app/api/query.py:288-290` drops unverified ones before they reach
the client). The review checklist has no equivalent — nothing stops a
hallucinated page/section reference from shipping as if it were confirmed.

The `review-files`/`pdfs` PDF-serving and viewing plumbing this needs
already exists and is proven out by a second feature, "chat over review
documents" (`SessionChat.tsx` + `POST /api/session/query`): a `Source`
carries `{label, page_pdf, section_id, doc_type}`
(`SessionChat.tsx:17-25`), `isPdfLinkable(source)` (`:49-50`) gates whether
it renders as a clickable `SourcePill` (`:69-105`) or a plain badge, and
clicking one opens the shared `PDFViewerModal.tsx` pointed at either the
public `/api/pdf/{doc_name}` (Standard Specifications /
Construction Scheduling Manual, no auth) or the private
`/api/review/{project_id}/pdf/{doc_type}` (`review.py:1359-1394`, JWT-authed,
ownership-checked) depending on the source. This spec wires the same
verified-citation pattern into the checklist's `CheckCard`
(`frontend/src/components/review/DocumentReview.tsx:179-219`), which today
renders `finding`/`reasoning`/`evidence` as plain text with no link at all.

## Goal

Each review check gets 0–N citation pills below its evidence text. A pill
for Spec/CSM, Special Provision, or Narrative evidence is clickable only if
the LLM's claimed source was matched against a passage that was actually
retrieved for that check (verified); if the LLM named a passage that
doesn't match anything retrieved, the pill still appears but flagged and
non-clickable, so a reviewer can see the AI tried to cite something
unconfirmed rather than have it silently vanish or silently link to a
possibly-wrong page. Key Map / Estimate checks always get one non-LLM,
whole-document pill (page 1) when that document was uploaded, since those
sources have no per-passage retrieval to verify against. Schedule-only
checks are unchanged — no PDF exists for XER data.

## Non-goals

- Key map / estimate passage-level retrieval — out of scope; their
  citation is always a whole-document, page-1 reference (per user
  decision during design).
- Any citation for `schedule`/`utility_plan`-sourced evidence — no PDF
  exists for either (XER data / a cross-reference bonus source that
  already degrades gracefully when absent, see `eval_engine.py:476-481`).
- Changing `_judge_grounding`'s contract or the judge/retry pipeline
  (`2026-09-08-grounding-judge-review-fixes-design.md`) — grounding
  judges "does the evidence support the verdict"; this feature judges
  "does the cited passage actually exist." Independent, layered checks.
- Dropping unverified citations (chat's behavior) — per user decision,
  review shows them flagged instead, since a reviewer auditing a
  compliance report benefits from seeing an unconfirmed claim rather
  than having it silently disappear.
- Any change to the jsPDF report export
  (`DocumentReview.tsx handleDownloadPdf()`) — citations are an
  interactive-UI feature; the exported PDF table stays exactly as it is
  today, still built from `category/name/reasoning/status/finding/evidence`.

## Design

### 1. Data model (`backend/app/models.py`)

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

`EvaluationSchema` (`models.py:102-108`) gains:

```python
    cited_chunk_ids: List[str] = []   # tags copied verbatim from the evidence block
```

`ReviewCheckResult` (`models.py:120-128`) gains:

```python
    citations: List[ReviewCitation] = []
```

One `kind` covers both existing PDF routes because `PDFViewerModal` already
switches on exactly this distinction (no `authToken`/`requireAuth` -> plain
`iframe src` for the public bucket; `authToken`+`requireAuth` -> authenticated
blob fetch for the private bucket) — see `PDFViewerModal.tsx:9-11,48-49`.

### 2. Tagging evidence so the LLM can name what it used

Today `sp_search_fn`/`spec_search_fn`/`csm_search_fn`
(`review.py:337-360,579-605`) and `build_narrative_text`
(`eval_engine.py:247-263`) return a plain joined `str`, discarding exactly
the `page_pdf`/`section_id` metadata a citation needs. Each becomes:

```python
@dataclass
class _EvidenceCandidate:
    kind:       Literal["public", "private"]
    doc_type:   str
    label:      str
    page_pdf:   Optional[int]
    section_id: Optional[str] = None

CitedSearch = Callable[[str], Tuple[str, Dict[str, _EvidenceCandidate]]]
```

Each search function tags every passage it returns inline, e.g.
`[cite:sp-0] <passage text>`, and returns a `{tag: _EvidenceCandidate}` dict
alongside the text:

- **`_build_sp_search_fn`** (`review.py:337-360`): `sp_chunks` already
  carries `metadata.page_pdf` per chunk (set by
  `chunk_special_provision`, `session_chunker.py:422-429`) — the cosine-sort
  currently keeps only `c["content"]` (`texts = [c["content"] for c in
  sp_chunks]`, `:352`); keep the whole chunk dict through the sort instead,
  tag each of the top-`k` as `sp-{i}`, `kind="private"`,
  `doc_type="special_provision"`, `label="Special Provision"`,
  `page_pdf=c["metadata"]["page_pdf"]`.
- **`_build_sp_search_fn_from_supabase`** (`:363-387`, the `reseed=False`
  path): `retrieve_sp_chunks` rows already carry `metadata.page_pdf`
  (confirmed by `build_sp_tool`, `sp_retriever.py:67-75`, which does the
  same extraction for chat) — same tagging, same candidate shape.
- **`_build_static_doc_search_fn`** (`:579-605`, backs both `spec_search_fn`
  and `csm_search_fn` via `_SPEC_COLLECTION`/`_CSM_COLLECTION`,
  `review.py:111-112,916-917`): `VectorSearcher.search()` results already
  carry `metadata.doc`/`metadata.section_id`/`metadata.page_pdf`
  (`vector_search.py:31-40` — this is the exact same metadata shape chat's
  `CitationSerializer` validates against). Tag each result `{collection}-{i}`,
  `kind="public"`, `doc_type=r["metadata"]["doc"]` (e.g. `"Spec2019"` —
  already the right key for `/api/pdf/{doc_name}`, per `pdf.py:24-27` and
  how chat already uses this field, `ChatInterface.tsx:596-605`),
  `label=r["metadata"].get("section_title") or r["metadata"]["doc"]`,
  `section_id=r["metadata"].get("section_id")`.
- **`build_narrative_text`** (`eval_engine.py:247-263`): the Cypher query
  (`:254-258`) selects `id, heading, text` but not `pagePdf`, even though
  `NarrativeChunk` nodes already store it (`graph_neo4j/seed.py:328-333`).
  Add `c.pagePdf AS pagePdf` to the `RETURN`, tag each row `narrative-{i}`,
  `kind="private"`, `doc_type="narrative"`, `label=r["heading"] or "Narrative"`,
  `page_pdf=r["pagePdf"]`.

`utility_plan_search_fn` is **not** changed — out of scope (Non-goals).

### 3. `_evaluate_one_check`: build tagged evidence, then validate the LLM's claims

`_evaluate_one_check` (`eval_engine.py:439-616`) currently takes
`sp_search_fn`/`spec_search_fn`/`csm_search_fn` typed `Callable[[str], str]`
and appends their return value straight into `evidence_parts`
(`:522-531`). Change:

```python
    evidence_parts: List[str] = []
    citation_lookup: Dict[str, _EvidenceCandidate] = {}

    def _add(text: str, candidates: Dict[str, _EvidenceCandidate]) -> None:
        evidence_parts.append(text)
        citation_lookup.update(candidates)

    if "schedule" in sources:
        evidence_parts.append(schedule_facts)
    if "narrative" in sources:
        _add(*narrative_result)          # (text, candidates) computed once in evaluate_checks
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
```

(`utility_plan_search_fn`'s call is untouched, still appended as plain text.)

The system prompt (`_STATIC_SYSTEM_PROMPT`, `:63-79`) gets one added rule:
"Some passages above are tagged like `[cite:sp-0]`. If your evidence draws
on a tagged passage, copy its tag id (the part after `cite:`, e.g. `sp-0`
— no brackets, no `cite:` prefix) into `cited_chunk_ids`. Passages without
a tag (schedule facts, key map facts, estimate facts) don't need one." Each
tag id in `citation_lookup` (below) is keyed by that same bare id (`"sp-0"`,
not `"[cite:sp-0]"` or `"cite:sp-0"`), so `citation_lookup.get(tag)` matches
directly on whatever the LLM copied — no parsing needed on the response side.

After the (possibly retried) `result: EvaluationSchema` is final, resolve
`result.cited_chunk_ids` against `citation_lookup`:

```python
    citations: List[ReviewCitation] = []
    for tag in result.cited_chunk_ids:
        cand = citation_lookup.get(tag)
        if cand is not None:
            citations.append(ReviewCitation(
                kind=cand.kind, doc_type=cand.doc_type, label=cand.label,
                page_pdf=cand.page_pdf, section_id=cand.section_id, verified=True,
            ))
        else:
            citations.append(ReviewCitation(
                kind="private", doc_type="unknown", label=f"Unverified citation ({tag})",
                page_pdf=None, section_id=None, verified=False,
            ))
    if "keymap" in sources and keymap_facts is not None:
        citations.append(ReviewCitation(
            kind="private", doc_type="key_map", label="Key Map",
            page_pdf=1, verified=True,
        ))
    if "estimate" in sources and estimate_facts is not None:
        citations.append(ReviewCitation(
            kind="private", doc_type="estimate", label="Estimate",
            page_pdf=1, verified=True,
        ))
```

This runs after the judge/retry block, using whichever `result` ends up
final (original, retried, or downgraded-to-Missing) — a downgraded/error
result naturally has `cited_chunk_ids=[]` (fresh `EvaluationSchema(...)`
construction, `:548-551,597-601,607-611`), so those checks correctly get
only the automatic key-map/estimate pills, no chunk claims. `citation_lookup`
is built once per check regardless of whether a retry happens (the retry
reuses the same tagged `user_msg`, `:567`), so a retried answer's citations
validate against the same lookup.

`ReviewCheckResult(...)` (`:613-616`) gains `citations=citations`.

### 4. Wiring in `evaluate_checks`

`build_narrative_text` now returns `(text, candidates)`; computed once
(`eval_engine.py:694`) and passed to every check requesting `"narrative"` —
same one-computation-shared-across-checks pattern already used for
`schedule_facts`/`narrative_text` (module docstring, `:24-28`).
`sp_search_fn`/`spec_search_fn`/`csm_search_fn` parameters on both
`_evaluate_one_check` and `evaluate_checks` are retyped
`Optional[CitedSearch]`.

### 5. API response shaping

`_to_frontend_shape()` (`review.py:682-709`) passes `citations` through
unchanged, per check:

```python
            {
                "id": c.id, "category": c.category, "name": c.name,
                "reasoning": f"Source: {c.source}",
                "status": _STATUS_MAP.get(c.status, "warning"),
                "finding": c.evidence, "evidence": c.evidence,
                "citations": [cit.model_dump() for cit in c.citations],
            }
```

### 6. Frontend (`DocumentReview.tsx`)

`CheckItem` (`:15-23`) gains:

```ts
  citations?: {
    kind: 'public' | 'private'
    doc_type: string
    label: string
    page_pdf?: number | null
    section_id?: string | null
    verified: boolean
  }[]
```

`CheckCard` (`:179-219`) is passed `authToken`/`projectId` (already held by
the parent component, `:272-273`, currently not threaded to `CheckCard`) and
renders a pill row below the `evidence` line, reusing `SessionChat.tsx`'s
`SourcePill` (`:69-105`) look: a clickable pill (`onClick` opens the modal)
when `citation.verified && citation.page_pdf != null`, otherwise a greyed,
non-clickable pill with a warning glyph and a
`title="AI referenced this but it couldn't be verified against retrieved evidence"`
tooltip. `CheckCard` owns its own `pdfCitation` state and renders
`PDFViewerModal` locally on click (mirrors how `SessionChat.tsx` owns its
own `pdfSource` state, `:167` — no lifting through `CollapsibleSection`
needed):

```tsx
url={citation.kind === 'public'
  ? `${API_BASE}/api/pdf/${citation.doc_type}`
  : `${API_BASE}/api/review/${projectId}/pdf/${citation.doc_type}`}
page={citation.page_pdf ?? 1}
authToken={citation.kind === 'private' ? authToken : undefined}
requireAuth={citation.kind === 'private'}
headerLabel={citation.label}
headerDetail={citation.section_id ?? undefined}
```

`CollapsibleSection` (`:221-254`) threads `projectId`/`authToken` down to
each `CheckCard` it renders (`:250`).

## Testing

- `backend/tests/test_eval_engine.py`: existing tests that mock
  `sp_search_fn`/`spec_search_fn`/`csm_search_fn` as `Callable[[str], str]`
  must update their fakes to the new `(str, dict)` return shape — traced
  by hand per-test during implementation, same as the grounding-judge pass
  did (`2026-09-08-grounding-judge-review-fixes-design.md`).
- New tests: a check whose LLM response cites a real tag -> `citations`
  contains one `verified=True` entry with the right `page_pdf`/`doc_type`
  from the candidate, not from anything the LLM said. A check whose
  response cites a made-up tag -> one `verified=False` entry, `page_pdf=None`.
  A check with `"keymap"`/`"estimate"` in `source_files` and that document
  present -> the automatic page-1 citation appears even when
  `cited_chunk_ids` is empty. A downgraded-to-Missing check -> `citations`
  contains only the automatic key-map/estimate entries (if any), no chunk
  claims (since the fallback `EvaluationSchema` has `cited_chunk_ids=[]`).
- Full backend suite must stay green.
- **Frontend:** no unit-test harness exists for `DocumentReview.tsx`
  (confirmed absent by the granular-progress spec,
  `2026-09-08-granular-review-progress-design.md`) — verify manually:
  `cd frontend && npx tsc --noEmit -p .` clean, then run a review with
  Spec/CSM/SP/Narrative/Key-Map/Estimate documents uploaded and confirm
  each shows the expected pill (clickable + opens at the right page, or
  flagged + non-clickable), against real Route 49 project data.

## Success criteria

Running a review that includes a Special Provision, Narrative, Key Map, and
Estimate upload shows citation pills on the relevant checks; clicking a
verified pill opens `PDFViewerModal` at the actual page the evidence came
from (confirmed against the source PDF, not just a plausible-looking
number); a check whose LLM response invents a citation shows a flagged,
non-clickable pill instead of either a broken link or a silently-dropped
claim.
