# Hybrid Check Retrieval — Design

**Date:** 2026-09-14
**Status:** awaiting user review
**Branch / worktree:** `rag-check-instructions-v3` at `.claude/worktrees/rag-check-instructions-v3`
**Related:** `2026-09-14-check-instructions-v3.md` (spec), `../plans/2026-09-14-check-instructions-v3-rag.md` (plan)

## Problem

Compliance checks that name a specific clause or table frequently do not receive
it. `working_drawing_review_time` names TABLE 105.05-1, `steel_pole_lead_time`
names CSM Table A, `narrative_night_work` names the safe-time clause, and each
depends on that text being in the evidence block. Today the text often is not,
and the check answers from whatever else it was given.

Three causes, all confirmed in the code on 2026-09-14.

**1. Special Provision chunks have no structure.** `chunk_special_provision`
(`backend/app/ingestion/session_chunker.py:354`) is a blind sliding window: 600
tokens, 100 of overlap, cut wherever the count lands. It attaches `doc_type`,
`page_pdf` and `chunk_index` and nothing else. A chunk routinely begins
mid-sentence inside 105.05 and ends inside 105.07, so nothing in the system
knows which clause any passage belongs to.

**2. The compliance path is the only consumer using dense-only retrieval.**

| Path | Documents | Retrieval today |
|---|---|---|
| Chat | static specs, manuals | hybrid: BM25 + RRF |
| Compliance, Special Provisions | per-project upload | dense only |
| Compliance, key map / estimate / utility plan | per-project upload | dense only |
| Compliance, specs and scheduling manual | static, same table chat reads | dense only |

The last row is the sharpest: compliance checks read the same static collection
the chat endpoint reads, but go through a bare `VectorSearcher`
(`backend/app/api/review.py:607`) rather than `HybridRanker`. A reviewer asking
about Table 105.05-1 through chat gets better retrieval than the check that
exists to evaluate it.

**3. The query is the wrong shape for either half.** The vector query is the
check instruction, roughly 150 words of rule text, which embeds far from a
600-token clause even when that clause is the right one. Naively handing the
same string to BM25 is worse: `websearch_to_tsquery` AND-chains every surviving
token, so a 150-word query matches zero rows.

## Decisions taken during brainstorming

| Question | Decision |
|---|---|
| Where does the keyword query come from | Regex extraction of anchors from the instruction, no schema change |
| How is a named anchor resolved | Metadata lookup, not raw text match |
| How is section metadata obtained | Section-aware chunking at ingestion, re-ingest going forward |
| Scope | All three layers in one spec, built in order |
| Placement | New compliance retrieval module, minimal change to the shared ranker |
| Environment | The user's existing local Supabase in Docker |

Two things were considered and rejected. Putting document identity such as
"special provision" into the keyword query was rejected because document type is
already a filter on every per-project search, and adding those words to an
AND-chained tsquery would exclude every chunk that does not literally contain
them. Adding an authored `bm25_query` field to each check definition was
rejected because it duplicates the instruction's anchors in a second place that
must be kept in sync, and the v3 instruction rewrite already puts those anchors
in the first sentence.

## Architecture

Three layers in front of the existing evaluation engine. Each handles what the
one before it cannot.

```
check.instruction
       |
       v
  extract_anchors()  ->  ["105.05", "TABLE 105.05-1"]
       |
       +--> Layer 2: exact metadata lookup  ------> pinned chunks
       |        section_id in anchors
       |        or tables overlapping anchors
       |
       +--> Layer 3: dense search (full instruction)  --+
       |                                                +--> RRF --> fused
       +--> Layer 3: BM25 search (anchor string only) ---+
                                                              |
                                   evidence = pinned ++ fused[: budget - |pinned|]
```

Layer 1 is what makes Layer 2 possible and runs at ingestion rather than query
time.

### Layer 1: section-aware chunking

`chunk_special_provision` gains section awareness using
`app.ingestion.section_detector.detect`, which already recognises every NJDOT
heading form in use: `SECTION 703 – HIGHWAY LIGHTING`, `105.07`, and
`105.07.02`. It is used by the static specifications ingestion, which is why
that table's chunks carry `section_id` and `section_title` while uploaded
Special Provisions carry neither. It was simply never applied to per-project
uploads.

Chunks split on detected section boundaries. Within a section longer than the
token budget the existing sliding window still applies, so a long clause becomes
several chunks that share one section identity. Table captions matching
`TABLE\s+[\d.\-]+` are collected per chunk into a `tables` list. The pdfplumber
table extraction already in the chunker is unchanged; only the caption is newly
recorded.

Resulting metadata:

| Field | Example | New |
|---|---|---|
| `doc_type` | `special_provision` | no |
| `page_pdf` | 14 | no |
| `chunk_index` | 7 | no |
| `section_id` | `105.05` | yes |
| `section_title` | `WORKING DRAWINGS` | yes |
| `tables` | `["TABLE 105.05-1"]` | yes |

This improves dense retrieval on its own, independently of Layers 2 and 3,
because a chunk stops straddling two unrelated clauses.

### Layer 2: exact anchor lookup

`extract_anchors` reads the instruction's first paragraph and returns section
and table identifiers. It reuses the patterns `section_detector` already
defines rather than inventing a second dialect. A check whose first paragraph
holds no anchor returns an empty list, which is not an error.

Chunks whose `section_id` matches an anchor, or whose `tables` list overlaps
one, are pinned into the evidence ahead of all ranked results. Pinning is a
metadata filter, so it is exact and carries no ranking variance at all. This is
the layer that answers the original question of actually getting the table.

Pinned chunks are capped at half the budget, so a long section cannot crowd out
everything else. Within the cap, chunks are ordered by `chunk_index` so a
multi-chunk table arrives in reading order.

### Layer 3: BM25 with rank fusion

`session_chunks` gets the full-text index and search function that `chunks`
already has, mirroring `keyword_search_chunks`. The keyword query is the anchor
string only, never the instruction, for the AND-chaining reason above. When
there are no anchors the keyword leg is skipped entirely and retrieval is dense
only, which is today's behaviour.

Fusion is Reciprocal Rank Fusion with the existing smoothing constant of 60.
`classify_query` already shifts weights to keyword-heavy when it sees a section
number or a table reference, which is exactly the anchored case, so it is reused
unchanged.

| Query type | Dense weight | Keyword weight |
|---|---|---|
| Anchored | 0.3 | 0.7 |
| Unanchored | 0.6 | 0.4 |

The same composition applies to the static specifications and scheduling manual
searches the compliance path uses, so those stop being dense-only too.

### Known limitation: phrase anchors are not implemented

Several checks in the catalog name a phrase rather than a section or table --
`narrative_night_work` and `utility_work_hours` both tell the evaluator to
search for `"safe-time"`; `utility_alignment` tells it to search for `"Work
to be Performed by Utility"`. Nothing in this design extracts those phrases
into a retrieval anchor. `extract_anchors` (`backend/app/compliance/anchors.py`)
recognises exactly two shapes -- section identifiers and table captions --
and nothing else; a quoted phrase in an instruction is never captured and
never reaches `keyword_search_session_chunks` as a query term. Where a check
like this still gets its passage, it is because the phrase happens to sit in
a section that is itself named nearby (`105.07.02`), so Layer 2's pinning
delivers it -- not because the keyword leg matched the phrase.

Extending `as_query()` to append phrases is not a small addition on top of
what exists. `websearch_to_tsquery` AND-chains every surviving token in a
single query string, so a query that already carries a section number (e.g.
`105.07.02`) and then gains a phrase (`safe-time`) would require both to
appear in the same chunk to match at all. That over-constrains the search
rather than widening it, and works against the reason the query is anchors-only
in the first place (see the module docstring above). A real fix needs either
a second, separate keyword query per phrase or an OR-combined tsquery, both
of which are out of scope here. This is a known gap, not a to-do: it is not
implemented and is not assumed by anything else in this design.

## Components

| File | Change | Responsibility |
|---|---|---|
| `backend/app/ingestion/session_chunker.py` | modify | Section-aware SP chunking, table captions |
| `backend/app/compliance/check_retrieval.py` | create | Anchor extraction, pinning, budget split |
| `backend/app/retrieval/hybrid_ranker.py` | modify | Extract a table-agnostic `fuse` |
| `backend/app/retrieval_langchain/sp_retriever.py` | modify | Caller-set similarity floor, metadata-filtered lookup |
| `backend/sql/session_chunks_hybrid.sql` | create | GIN index, `keyword_search_session_chunks` |
| `backend/sql/local_setup.sql` | modify | Add `session_chunks` and its indexes and functions |
| `backend/app/api/review.py` | modify | Both SP closures delegate to `check_retrieval` |

The new module keeps check-specific logic out of both the shared ranker, which
the chat path depends on, and `review.py`, already the largest file in the API
package. Fusion is generalised only enough to accept any vector and keyword
pair.

Delegating both SP closures to one module also removes a live bug. The
fresh-review closure (`review.py:345`) applies no similarity floor and always
returns `top_k` passages; the rerun closure (`review.py:382`) applies 0.2 and
returns nothing when no chunk clears it. The same check therefore retrieves
differently depending on which code path ran, which is what made verdicts flap
between runs on identical input.

## Failure behaviour

**A project ingested before this change has no section metadata.** Its chunks
are flat, so pinning finds nothing. That must not be reported as a missing
clause, because the document may well contain it. The lookup distinguishes the
two cases by checking whether any chunk in the project carries `section_id` at
all. No section metadata anywhere means the project predates the change, and
retrieval degrades silently to fusion. Section metadata present but no match
means the anchor genuinely is not in the document, which is worth surfacing
through the `insufficient_evidence` flag.

**No anchors in the instruction** skips Layers 2 and 3's keyword leg and runs
dense only.

**An empty retrieval** is already handled on this branch: both closures return a
known sentinel and the engine reports `Missing` rather than answering from the
remaining sources.

## Local environment

The user runs Supabase locally in Docker. Configuration lives in
`backend/local.env.local`, which defines `SUPABASE_URL` and
`SUPABASE_SERVICE_ROLE_KEY` among twelve variables. Its values are never read
or printed by this work.

Three practical consequences.

1. That file is gitignored and therefore absent from the worktree. Working in
   the worktree requires copying it and `backend/.env` across, or pointing the
   loader at the main checkout.
2. `local.env.local` carries no `OPENAI_API_KEY`, which `backend/.env` does. The
   loader must read `.env` first and overlay `local.env.local`, so the local
   database is used with the real embedding key.
3. Neither file defines `DATABASE_URL`, so `backend/scripts/deploy_sql.py`
   cannot connect. New SQL is applied through the local Studio SQL editor, or
   `DATABASE_URL` is added for the local container.

All ingestion, retrieval and measurement in this design run against that local
instance. No hosted data is read or written.

## Testing

**Unit, no database.** Anchor extraction across every catalog instruction.
Fusion arithmetic against a worked example. The budget split, including the
pinned cap and deduplication between pinned and fused results. Degradation when
chunks carry no section metadata. Section-aware chunking against a fixture page
sequence, asserting that a chunk never spans two detected sections.

**Integration, local database.** Ingest a known Special Provision, then assert
that a check naming TABLE 105.05-1 receives the chunk containing it, at a fixed
position, on ten consecutive runs. This is the acceptance test, and it is the
one that cannot be faked, because it is the only way to verify that
`websearch_to_tsquery` still matches a token like `105.05-1` after its own
tokenisation.

**Measurement.** The retrieval probe already specified in the v3 plan is
extended with two columns per check: whether an anchor was extracted, and
whether it was pinned. Before and after runs are compared on the same project.

## Success criteria

1. A check naming a section or table receives it, on every run, whenever the
   document contains it.
2. `no_concrete_winter` and the other flapping checks return identical verdicts
   across three consecutive runs on identical input.
3. Anchored checks show a higher top similarity and a higher anchor-in-top-
   passage rate than the recorded baseline.
4. No regression in the chat path, which keeps its own similarity floor and its
   existing ranker behaviour.

## Out of scope

Re-ingesting Special Provisions for existing projects, which happens naturally
on the next upload. Changing the chat endpoint's retrieval. Reranking with a
cross-encoder model, which is a plausible fourth layer but adds a dependency and
should be judged against this design's measurements first. Any change to the
output schema or to the set of 57 checks.

## Open items

None blocking. The instruction rewrite in the companion v3 plan and this design
touch the same first paragraph of each `sp` instruction, so the anchor-first
rewrite should land before or with Layer 2, otherwise `extract_anchors` has less
to find.
