# Hybrid Check Retrieval — Before/After Results

**Date:** 2026-09-14
**Status:** measured
**Related:** `../specs/2026-09-14-hybrid-check-retrieval-design.md` (design, success criteria quoted below), `.superpowers/sdd/task-11-brief.md` + its two corrections (method)

This is Task 11: measurement only. No retrieval, ingestion, or engine code was
changed to produce these numbers.

## Method (read this before the numbers)

The brief's original before/after plan assumed re-ingesting a Special
Provision PDF through the review pipeline. No PDF is available in this
environment, so the actual method (per the task's Correction 2) was:

1. **Before corpus.** The local database already holds a real, pre-existing
   project — `eaf91d03-ae9f-40e2-a22f-26e5730186a0`, Route 49 / Bridge over
   Maurice River, Contract No. 036153140 — with 245 `special_provision`
   chunks whose metadata carries only `doc_type`, `page_pdf`, `chunk_index`.
   No `section_id`. This is real production text ingested before Task 6's
   section-aware chunker existed, and it was not modified.
2. **Stitching.** Those 245 chunks were read back in `chunk_index` order and
   concatenated, undoing the sliding-window overlap between adjacent chunks
   by finding the longest exact token-for-token match between each
   chunk's tail and the next chunk's head (see
   `backend/scripts/rebuild_sp_after_corpus.py::stitch`). Empirically, of
   the 244 adjacent pairs, 96 had a real overlap (98–100 tokens) and 148 had
   none — consistent with some form of run/section-reset chunking having
   produced this baseline, not a single continuous sliding window, though
   its exact original chunker is not recoverable from the stored metadata
   alone. The exact-match approach never duplicates and never silently
   drops text: where no match is found, nothing is removed.
3. **After corpus.** The stitched ~137k-token text was run through the
   *current* `app.ingestion.session_chunker.chunk_special_provision`
   (unmodified) and inserted under a fresh session id,
   `11111111-2222-4333-8444-555555555555`, via the same
   `insert_session_chunks` every real ingestion path uses. This produced 514
   chunks, 511 of which carry a `section_id`.
4. Both corpora were measured with `backend/scripts/sp_retrieval_probe.py`
   (newly created for this task — it did not exist before).

**What this method distorts, honestly:**

- The stitched text was handed to `chunk_special_provision` as **one
  synthetic page**, not ~165 real PDF pages (page boundaries were lost when
  the stored chunks were stitched back together — only `page_pdf` per
  *chunk*, not per line, survived). Consequences: (a) every "after" chunk's
  `page_pdf` metadata reads `1` — wrong, but the probe never reads it, only
  `section_id`/`tables`/similarity; (b) `_detect_sp_boilerplate` samples the
  first 15 *pages* for text that repeats across pages, and with one page it
  can never find a repeat, so no boilerplate stripping happens for the after
  corpus. This is not a regression: the baseline text already carries
  unstripped boilerplate itself (literal `"Page 2 of 103"` sits inline in
  its chunk content), because the per-page footer text changes on every page
  and was never caught by the exact-match boilerplate detector regardless of
  which chunker ran.
- **Single project.** Every number below is from one project. No claim here
  generalizes to "SP documents in general" without more projects measured.
- **No LLM calls were made.** The probe calls `retrieve_for_check` and
  `retrieve_sp_chunks` directly — embeddings and Postgres only. It does not
  run the full compliance engine (`evaluate_checks`), so nothing here
  measures whether a check's final Pass/Fail/Missing verdict changed, only
  what evidence it would have received.
- The after-corpus rows were deleted after this measurement (see
  **Reproduction** below) — task instructions require cleaning up rows
  inserted under a session id created for this task unless there's a reason
  to keep them, and there wasn't one once the JSON results were saved to
  `backend/data/eval/`.

## Headline measurements

1. **18 of 22** Special-Provision-scoped checks extract an anchor from their
   instruction (near the brief's "18 of 21" expectation — this catalog has
   22 `sp`-scoped checks, one more than the brief assumed).
2. **Anchor pinned in the top passage:** 14/18 anchored checks before → 16/18
   after (2 flipped False→True: `water_interruption`, `steel_pole_lead_time`;
   0 regressed). The other 14 already showed the right passage on top
   *before* pinning existed, via dense+keyword fusion alone.
3. **Stability across 5 repeated runs:** 22/22 checks returned an identical
   top row every time, in *both* the before and after corpus. See Criterion
   2 below for why this is weaker evidence than it sounds.
4. **Top dense similarity increased for 15/18 anchored checks** (3 did not:
   `railroad_restrictions`, `controller_lead_time`, `nearby_projects` — see
   "What did not improve"). Averaged across all 22 SP checks (anchored and
   not), similarity increased for 15/22 and decreased for the 4 non-anchored
   checks plus those same 3.

## Criterion-by-criterion (design doc's four success criteria)

### 1. "A check naming a section or table receives it, on every run, whenever the document contains it."

**Mostly met, with two named exceptions.** Of the 18 anchored checks, 16
receive their named section/table pinned into the evidence after the
change (verified against `pin_by_anchors`' own exact-match rule, not just
inferred from a rank). The 2 that don't:

- **`railroad_restrictions`** names `105.07` (bare). This project's SP text
  only ever headers `105.07.01` and `105.07.02` — `105.07` on its own never
  appears as a `section_id` anywhere in either corpus (verified directly
  against the after-corpus's 279 distinct section ids). `pin_by_anchors`
  does exact `section_id` equality, not a prefix match, so a
  coarser-than-the-document anchor can never pin, even though the document
  plainly does discuss `105.07`-family utility content. **This is a real
  gap in the pinning layer for this check, not a data problem.**
- **`nearby_projects`** names `105.06`. This project's SP genuinely contains
  no `105.06` anywhere — not as a heading, not as literal text (checked with
  a direct substring search across all 514 after-corpus chunks: 0 hits).
  `retrieve_for_check`'s `anchor_missing` flag correctly reports `True` for
  this check in the after run, which is the intended behavior (the engine
  short-circuits to "Missing" naming the absent clause instead of asking an
  LLM to answer from irrelevant evidence) — **this is the design working as
  intended, not a retrieval failure.**

So one exception is a real limitation worth fixing later (exact-match
granularity), and the other is the system correctly reporting a genuine
absence. They should not be averaged together.

### 2. "Previously flapping checks return identical verdicts across three (repeated) runs."

**Partially demonstrated, and the strongest evidence for this criterion
isn't the repeat-run test.** Two separate things are true here:

- **Structural fix, verified by code reading.** The design doc identifies
  the actual flapping mechanism: `review.py`'s fresh-review SP closure
  applied no similarity floor while its rerun closure applied 0.2, so the
  same check retrieved different evidence depending on which code path ran
  on a given call. Reading `backend/app/api/review.py` today:
  `_build_sp_search_fn` (fresh upload, in-memory) and
  `_build_sp_search_fn_from_supabase` (rerun, database-backed) both now
  apply the same anchor extraction and the same pin budget
  (`PIN_BUDGET_FRACTION`) before ranking the remainder — this is the actual
  fix for the bug the criterion describes. **Caveat:** these are two
  separately-maintained implementations that *agree* on pin logic (per
  their own docstrings), not one shared function, and the fresh-upload path
  has no BM25/keyword leg at all (there's no full-text index over in-memory
  chunks) while the rerun path does — so they are unified on pinning, not
  identical end to end. I read this code; I did not exercise the
  fresh-upload path (it needs a real PDF upload through the API, which
  wasn't available).
- **Repeat-run measurement, weaker than it looks.** `--repeat 5` on
  `retrieve_for_check` showed every one of the 22 checks returning the exact
  same top row id on all 5 runs, in both the before and after corpus. The
  "before" corpus was *also* 100% stable — meaning this single project's
  data never actually exhibited flapping to begin with, in either corpus,
  so this run doesn't demonstrate an improvement from instability to
  stability. What it does confirm structurally: pinned rows come from an
  exact SQL `.in()` filter, sorted by `chunk_index` in Python — that path
  has no source of run-to-run variance by construction, unlike the fused
  (ranked) remainder, which stays exposed to floating-point drift in
  repeated embedding calls and to score ties. I could not find or construct
  a genuinely flapping case in this project's data to test that claim
  directly.

### 3. "Anchored checks show a higher top similarity and a higher anchor-in-top-passage rate than the recorded baseline."

**Anchor-in-top-passage: met** (14/18 → 16/18, 0 regressions — see headline
#2). **Top similarity: met for 15 of 18 anchored checks, not for 3.** The
"top similarity" metric here is a pure dense-search score
(`retrieve_sp_chunks(..., match_count=1)` on the check's full instruction,
independent of pinning) — it measures embedding proximity, not whether the
final evidence set is better. It moved down for `railroad_restrictions` and
`nearby_projects` (the same two that never pin — see Criterion 1) and, more
interestingly, for `controller_lead_time` (0.641 → 0.617) *despite* that
check successfully pinning 2 rows after the change and keeping
`anchor_in_top_passage = True` in both runs. That's a case where the
production-relevant signals (pin count, anchor-in-top) improved or held
while the pure similarity proxy moved the other way — a reminder that these
are genuinely different measurements, not two views of the same thing.

### 4. "No regression in the chat path, which keeps its own similarity floor and its existing ranker behaviour."

**Met, verified by code inspection (not by running a live chat query).**
`backend/app/api/session.py`'s chat SP search imports `build_sp_tool` and
`retrieve_sp_chunks` directly from `app.retrieval_langchain.sp_retriever` —
grepping the whole `app/` tree, `check_retrieval.retrieve_for_check`,
`pin_by_anchors`, and `anchors.extract_anchors` are imported only by
`app/api/review.py` and `app/compliance/*`. Chat never touches the new
pinning/anchor/fusion-for-compliance code at all; it still calls
`retrieve_sp_chunks(..., match_threshold=0.2)` exactly as before this
feature existed. One nuance worth stating plainly rather than glossing
over: `retrieve_sp_chunks` itself is a function chat and compliance both
call, and its `_DOC_TYPE_OVERFETCH` cross-doc-type-limit fix (called out in
the brief as a separate, already-applied fix) changed *for both callers* —
so "chat's retrieval interface is unchanged" is accurate, but "not one line
touched by anything chat depends on" is not; the shared helper picked up a
bug fix that can only improve chat's recall, never regress it, and chat
still never goes through pinning, anchor extraction, or RRF fusion.

## Full per-check table

`sp_top_k` is each check's own configured retrieval budget. `pin`, `rows`,
`top_sim`, and `anchor_in_top` are `before → after`. `stable` reports
whether the top row id was identical across all 5 repeated runs, in both
corpora (`before/after`).

| check_key | anchor | sp_top_k | pin | rows | top_sim | anchor_in_top | stable (5×) |
|---|---|---|---|---|---|---|---|
| utility_alignment | 105.07.01, 105.07.02 | 12 | 0→6 | 12→11 | 0.564→0.575 | True→True | True/True |
| row_availability | 108.12 | 8 | 0→1 | 8→7 | 0.599→0.607 | True→True | True/True |
| environmental_permit | — | 8 | 0→0 | 8→8 | 0.561→0.549 | False→False | True/True |
| gas_interruption | 105.07.02 | 8 | 0→4 | 8→6 | 0.575→0.586 | True→True | True/True |
| water_interruption | 105.07.02, 651.03.01 | 8 | 0→4 | 8→8 | 0.559→0.579 | **False→True** | True/True |
| electric_interruption | 105.07.02 | 8 | 0→4 | 8→8 | 0.564→0.581 | True→True | True/True |
| utility_work_hours | 105.07.02 | 8 | 0→4 | 8→8 | 0.473→0.528 | True→True | True/True |
| railroad_restrictions | 105.07 | 8 | 0→0 | 8→8 | 0.476→0.473 | **False→False** | True/True |
| working_drawing_review_time | 105.05, TABLE 105.05-1 | 12 | 0→1 | 12→11 | 0.546→0.610 | True→True | True/True |
| steel_pole_lead_time | SECTION 702, SECTION 703, TABLE A | 8 | 0→3 | 8→8 | 0.503→0.540 | **False→True** | True/True |
| aluminum_pole_lead_time | SECTION 703, 703.03.07, TABLE A | 8 | 0→3 | 8→6 | 0.523→0.594 | True→True | True/True |
| controller_lead_time | 702.03.01, TABLE A | 8 | 0→2 | 8→7 | 0.641→0.617 | True→True | True/True |
| edq_items | — | 8 | 0→0 | 8→8 | 0.550→0.546 | False→False | True/True |
| narrative_row_requirements | 108.12 | 8 | 0→1 | 8→7 | 0.528→0.544 | True→True | True/True |
| narrative_work_hour_restrictions | 108.08, SECTION 159, 108.06 | 8 | 0→4 | 8→6 | 0.612→0.650 | True→True | True/True |
| narrative_night_work | 105.07.02 | 12 | 0→6 | 12→12 | 0.487→0.523 | True→True | True/True |
| traffic_control_staging | SECTION 703 | 12 | 0→1 | 12→12 | 0.575→0.621 | True→True | True/True |
| summer_shutdown | — | 8 | 0→0 | 8→8 | 0.467→0.460 | False→False | True/True |
| required_activities_present | — | 8 | 0→0 | 8→8 | 0.518→0.505 | False→False | True/True |
| multi_year_funding | 108.10 | 8 | 0→1 | 8→7 | 0.533→0.616 | True→True | True/True |
| nearby_projects | 105.06 | 8 | 0→0 | 8→8 | 0.563→0.554 | **False→False** | True/True |
| its_burn_in | 701.03.15, SECTION 704 | 8 | 0→1 | 8→7 | 0.569→0.576 | True→True | True/True |

## What did NOT improve — named, not averaged away

- **Exact-match anchor granularity (`pin_by_anchors`).** `railroad_restrictions`
  never pins in either corpus because its extracted anchor (`105.07`) is
  coarser than any `section_id` this document actually has
  (`105.07.01`/`105.07.02` only). Any check whose instruction names a
  parent section while the document only headers its children will hit this
  same gap. Not fixed here — this task measures, it doesn't patch.
- **`TABLE A` is extracted as an SP anchor for three checks
  (`steel_pole_lead_time`, `aluminum_pole_lead_time`, `controller_lead_time`)
  but "Table A" in their instructions refers to the Construction Scheduling
  Manual's Table A, a different document `pin_by_anchors` never searches
  (it's scoped to `doc_type="special_provision"`).** Oddly, this project's
  own SP text happens to contain an unrelated table also captioned
  `"TABLE A"` (confirmed present in the after-corpus's table-caption list),
  so a pin attempt on that anchor could in principle match the *wrong*
  table. The probe doesn't break down which specific anchor drove a given
  pin, so this is reported as an observed hazard, not a confirmed
  mis-pin — but it's a design gap worth flagging: `extract_anchors` has no
  concept of which document an anchor belongs to.
- **Pure dense-similarity for unanchored checks got slightly worse, not
  better.** All 4 non-anchored checks (`environmental_permit`, `edq_items`,
  `summer_shutdown`, `required_activities_present`) show *lower*
  top-similarity after re-chunking (e.g. `environmental_permit`
  0.561→0.549). A plausible reading: the new chunker's smaller,
  single-section chunks are individually less likely to accidentally cover
  a broad, anchor-less query than the old chunker's larger multi-topic
  windows sometimes did by chance. This is a real, if small, cost of finer
  granularity for exactly the checks that can't benefit from pinning.
- **Criterion 2's repeat-run test proved nothing about instability**, because
  the baseline was already 100% stable on this project's data (see
  Criterion 2 above) — the improvement claim rests on the structural code
  fix, which I verified by reading, not by reproducing the original bug.

## Reproduction

```bash
cd backend

# Before (pre-existing production project; DO NOT modify or delete its rows)
PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe scripts/sp_retrieval_probe.py \
    --project-id eaf91d03-ae9f-40e2-a22f-26e5730186a0 --label before-hybrid --repeat 5

# After (rebuild a fresh stitched+rechunked corpus under a new session id, then probe it)
PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe scripts/rebuild_sp_after_corpus.py \
    --from-session eaf91d03-ae9f-40e2-a22f-26e5730186a0 --new-session <a-fresh-uuid>
PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe scripts/sp_retrieval_probe.py \
    --project-id <same-fresh-uuid> --label after-hybrid --repeat 5

PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe scripts/sp_retrieval_probe.py \
    --compare before-hybrid after-hybrid
```

The after-corpus session used for this report
(`11111111-2222-4333-8444-555555555555`) was deleted from `session_chunks`
after the JSON results were written to `backend/data/eval/` (that directory
is gitignored — see `backend/.gitignore` — so the raw run data lives locally
only, not in this commit). The before project's rows were never modified.

The hermetic suite (`PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe -m pytest backend/tests -q`)
passed at 245/245, including the 4 integration tests, both before and after
this measurement work.
