# Debug Scripts for NJDOT Review & Query Pipelines

## Context

Three pipelines to instrument:

1. **Review pipeline** (`backend/app/api/review.py`): XER + PDF → GPT-4o → 30 compliance check results. No RAG chunks — full document sent to LLM.

2. **Session Q&A pipeline** (`backend/app/api/session.py`): Ingests project docs as chunks into `session_chunks` Supabase table. Queries via `match_session_chunks` RPC + permanent `scheduling` collection. Uses `session_chunker.py` (has `xer_to_chunks`, `chunk_narrative`, `chunk_special_provision`).

3. **Main RAG pipeline** (`backend/app/api/query.py`): Multi-query expansion → hybrid RRF search → cross-ref follow-up → BDC amendments → LLM. Queries permanent `chunks` table.

## Files to Create

All scripts in `backend/debug/`:

| File | Purpose |
|------|---------|
| `backend/debug/__init__.py` | Empty, makes it a package |
| `backend/debug/review.py` | Review compliance run + session ingestion + session management |
| `backend/debug/session_query.py` | Debug queries against a session (RAG from uploaded docs) |
| `backend/debug/query.py` | Debug queries against main RAG |

No changes to any existing production files.

---

## Script 1: `backend/debug/review.py`

**Usage:**
```
python -m debug.review schedule.xer narrative.pdf [--sp special_provision.pdf] [--show-prompt]
python -m debug.review --delete SESSION_ID
python -m debug.review --list
```

`--sp` is optional. If provided, the special provision PDF is chunked with `chunk_special_provision()` and added to the session alongside the narrative and XER chunks.

### Mode 1 — Run review + ingest session

Print every intermediate in order:

**STAGE 1 — FILE READING**
- XER file path + size in bytes
- Narrative PDF path + size in bytes
- SP PDF path + size in bytes (or "not provided" if `--sp` omitted)
- XER decoded text length (characters)

**STAGE 2 — XER PARSING** (calls `parse_xer_to_json()`)
- Total activities count
- Activity counts by type: milestone (duration=0) vs regular
- Top-level WBS phases found (unique first elements of wbs_path)
- Activities with negative total_float (list their IDs + float value)
- Activities with missing start/finish dates
- Full pretty-printed JSON of the FIRST 5 activities (complete dict, not truncated)
- Full pretty-printed JSON of ALL milestone activities (M100, M950, any duration=0)

**STAGE 3 — PROMPT ASSEMBLY**
- Length of schedule_json_str (characters + approximate tokens)
- Narrative PDF base64 string length
- If `--show-prompt`: print the full `_SYSTEM_PROMPT` text (134 lines)
- If `--show-prompt`: print the full user message (XER JSON blob + _USER_TEXT)

**STAGE 4 — LLM CALL**
- Model being called (GPT-4o or Claude fallback)
- Timestamp before call
- Timestamp after call + elapsed seconds

**STAGE 5 — RAW LLM RESPONSE**
- Print the complete raw text response string from the LLM (unedited)
- Length in characters

**STAGE 6 — PARSED CHECK RESULTS** (all 30 checks, one block each)
For every check:
```
══ CHECK [03/30]  id: ad_to_bid_gap  category: Administrative Dates ══
  NAME:      15 Business Days: Advertisement to Bid
  STATUS:    ✓ PASS   (or ✗ FAIL / ⚠ WARNING)
  REASONING:
    [full multi-line reasoning text from LLM, indented]
  FINDING:
    [single sentence]
  EVIDENCE:
    [quoted activity IDs, dates, durations]
```

**STAGE 7 — SUMMARY**
```
  Passed:        14
  Warnings:       8
  Failed:         5
  Manual Review:  3
  Model used:    gpt-4o
```

**STAGE 8 — SESSION INGESTION**

Sub-stage 8a — Narrative chunking:
- Calls `PDFParser(tmp_path).extract_text()` → prints page count, pages with text, pages filtered as Gantt
- Calls `chunk_narrative(pages)` → prints each chunk with:
  - Chunk index, doc_type, page_pdf, section_heading
  - Full content text

Sub-stage 8b — XER chunking:
- Calls `xer_to_chunks(activities)` → prints each chunk with:
  - Chunk index, doc_type, phase/activity_id
  - Full content text

Sub-stage 8c — SP chunking (new):
- Only runs if `--sp` was provided; prints "skipped" otherwise
- Calls `PDFParser(tmp_sp_path).extract_text()` → prints page count
- Calls `chunk_special_provision(pages)` → prints each sliding-window chunk with:
  - Chunk index, page_pdf, chunk_index
  - Full content text

**Session ID banner** — before the chunking sub-stages begin and again after insert:
```
████████████████████████████████████████████████████████████████████████████████
SESSION ID:  a1b2c3d4-e5f6-7890-abcd-ef1234567890
████████████████████████████████████████████████████████████████████████████████
```
The ID is printed twice so it's easy to find at the top and bottom of the Stage 8 output.

Sub-stage 8d — Embedding (formerly 8c in original plan):
- Total chunk count going to embedder
- Per-batch progress (batch N/M, chunk range)
- Embedding model used, output dimensions

Sub-stage 8e — Supabase insert (formerly 8d):
- session_id (UUID) assigned
- Per-batch insert confirmation (rows N-M / total)
- Final session ID banner (same `█` style as above)
- Chunk count breakdown: narrative / xer / sp
- Saves `sp_file` field to `debug/sessions.json` (null if not provided)

---

### Mode 2 — Delete session
- Query `session_chunks` table for count with this session_id
- Delete all rows
- Print count deleted
- Remove from `debug/sessions.json`

### Mode 3 — List sessions
- Read `debug/sessions.json`
- For each session: query Supabase for live chunk count
- Print table: session_id | project | created_at | chunk_count | files

---

## Script 2: `backend/debug/session_query.py`

**Usage:**
```
python -m debug.session_query SESSION_ID "question" [--match-count 8] [--show-prompt]
```

**STAGE 1 — SESSION CHECK**
- Query `session_chunks` table for this session_id
- Print total chunk count + breakdown by doc_type

**STAGE 2 — QUESTION EMBEDDING**
- Print question text
- Print embedding model + dimension (1536)
- Print first 10 values of the embedding vector (to confirm it was created)

**STAGE 3 — SESSION CHUNK SEARCH** (calls `match_session_chunks` RPC)
- Print the exact RPC parameters sent (session_id, match_count, match_threshold=0.2)
- For each row returned, a block:
  ```
  ── SESSION CHUNK [rank 1]  doc_type: xer_activities  similarity: 0.847 ──
    phase:         Pre-Stage 1A
    activity_id:   M100
    page_pdf:      —
    CONTENT (full):
      Milestone: Advertisement Date (ID: M100) is scheduled for...
    METADATA (full):
      {complete metadata dict}
  ```

**STAGE 4 — SCHEDULING MANUAL SEARCH** (calls `match_chunks` RPC, collection=scheduling)
- Print the exact RPC parameters sent
- For each row returned, a block:
  ```
  ── MANUAL CHUNK [rank 1]  section: 7.2  similarity: 0.721 ──
    doc:           SchedulingManual
    section_title: Working Day Limitations
    page_pdf:      45
    context_summary: [if present]
    CONTENT (full):
      [full chunk text]
  ```

**STAGE 5 — PROMPT ASSEMBLY**
- Print total context length (characters)
- If `--show-prompt`: print the full `_QA_SYSTEM` prompt
- If `--show-prompt`: print the full user message (all context blocks + question)

**STAGE 6 — LLM CALL**
- Model, timestamp before, timestamp after + elapsed

**STAGE 7 — RAW LLM RESPONSE**
- Complete raw text response (unedited)

**STAGE 8 — FINAL ANSWER + SOURCES**
- Clean answer text
- Sources list: label, heading, page_pdf, similarity for each source

---

## Script 3: `backend/debug/query.py`

**Usage:**
```
python -m debug.query "question" [--collection specs_2019|scheduling|material_procs] [--show-prompt]
```

**STAGE 1 — QUERY CLASSIFICATION** (calls `classify_query()`)
- Query text
- Query type: keyword-heavy / mixed / semantic
- Vector weight, keyword weight

**STAGE 2 — QUERY EXPANSION** (calls `QueryExpander._generate_variants()`)
- Prints the LLM prompt sent to generate variants
- Prints the raw LLM response for variant generation
- Prints all 3 query variants:
  - original
  - spec_style (LLM rephrasing)
  - keywords (3-6 core nouns)

**STAGE 3 — PER-VARIANT HYBRID SEARCH** (for each of 3 variants)
Calls `HybridRanker.search(variant, debug=True)`:
For each chunk returned:
```
── VARIANT "keywords" → CHUNK [rank 2] ──
  section_id:     902.02.03
  section_title:  Mix Design
  doc:            Spec2019
  page_pdf:       441 / page_printed: 407
  vector_rank:    3
  keyword_rank:   1
  rrf_score:      0.01234
  query_count:    —  (filled after merge)
  context_summary:
    Spec2019 section 902.02.03 Mix Design specifies Superpave gyratory...
  CONTENT (full):
    [complete chunk text]
```

**STAGE 4 — MULTI-RRF MERGE** (calls `QueryExpander._multi_rrf_merge()`)
- Shows deduplication: how many chunks had the same section_id (kept highest-scoring)
- Final ranked list with: rank, section_id, rrf_score, query_count (1/2/3)

**STAGE 5 — CROSS-REFERENCE FOLLOW-UP** (calls `_fetch_cross_ref_chunks()`)
- Section numbers found in retrieved chunk text (regex matches)
- Which are already in results (skipped) vs new
- For each new section fetched: the keyword search results + chunks added

**STAGE 6 — BDC AMENDMENTS** (calls `BDCMatcher.get_amendments()`)
- Section IDs queried
- For each amendment found:
  ```
  ── AMENDMENT: BDC25S-01  section: 902.02  type: substitution  code: ROUTINE ──
    subject:        Updated HMA specifications
    effective:      2025-03-01
    TEXT (full):
      [full amendment_text]
  ```
- If none found: "No BDC amendments found for retrieved sections"

**STAGE 7 — PROMPT ASSEMBLY** (calls `PromptBuilder.build()`)
- Chunk count going to prompt, amendment count
- Prompt structure summary (N chunks + M amendments)
- If `--show-prompt`: print full system prompt + user message

**STAGE 8 — LLM CALL** (calls `LLMClient.complete()`)
- Provider + model
- Timestamp before, after, elapsed seconds

**STAGE 9 — RAW LLM RESPONSE**
- Complete raw JSON text (unedited)

**STAGE 10 — CITATION VALIDATION** (calls `CitationSerializer.serialize()`)
For each citation the LLM produced:
```
── CITATION [1] ──
  LLM claimed:   section=902.02.03  page_printed=407  chunk_id=abc123
  Match method:  chunk_id match
  Verified:      ✓ TRUE
  Corrected:     page_printed 407→407 (no change)  page_pdf 441→441 (no change)
```

**STAGE 11 — FINAL ANSWER**
- Clean answer text
- Citations: document, section, page_printed, page_pdf, chunk_id, verified

---

## Session State File

`backend/debug/sessions.json`:
```json
{
  "sessions": [
    {
      "session_id": "uuid-here",
      "created_at": "2026-07-06T12:34:56",
      "xer_file": "schedule.xer",
      "narrative_file": "narrative.pdf",
      "chunk_count": 47
    }
  ]
}
```

---

## Shared Utilities (in `backend/debug/__init__.py` or a `_utils.py`)

- `section(title)` — prints `═══ TITLE ═══` separator
- `subsection(title)` — prints `── title ──`
- `colorize(text, color)` — ANSI codes: green=pass, red=fail, yellow=warning
- `sys.path` setup — adds `backend/` parent to sys.path so `app.*` imports work
- `load_env()` — calls `dotenv.load_dotenv()` from the `backend/` directory

---

## Critical Files Referenced

| File | What to import |
|------|---------------|
| `backend/app/api/review.py` | `parse_xer_to_json`, `_call_openai`, `_call_anthropic`, `_SYSTEM_PROMPT`, `_USER_TEXT` |
| `backend/app/api/session.py` | `_QA_SYSTEM`, `_DOC_TAG`, `_DOC_LABEL`, `_insert_chunks` (for the RPC logic) |
| `backend/app/api/query.py` | `_fetch_cross_ref_chunks`, `_RETRIEVE_K`, `_MAX_XREFS` |
| `backend/app/ingestion/session_chunker.py` | `xer_to_chunks`, `chunk_narrative`, `chunk_special_provision` |
| `backend/app/ingestion/pdf_parser.py` | `PDFParser` |
| `backend/app/ingestion/embedder.py` | `Embedder` |
| `backend/app/retrieval/query_expander.py` | `QueryExpander` — call `_generate_variants`, `_search_all_variants`, `_multi_rrf_merge` |
| `backend/app/retrieval/hybrid_ranker.py` | `HybridRanker`, `classify_query` — use `debug=True` for ranks |
| `backend/app/retrieval/bdc_matcher.py` | `get_bdc_matcher` |
| `backend/app/generation/prompt_builder.py` | `PromptBuilder` |
| `backend/app/generation/llm_client.py` | `LLMClient` |
| `backend/app/generation/citation_serializer.py` | `CitationSerializer` |
| `backend/app/database.py` | `get_db` |

---

## Verification

From the `backend/` directory:
1. `python -m debug.review schedule.xer narrative.pdf` — all 8 stages including full chunk content per session chunk
2. `python -m debug.review --list` — shows stored sessions
3. `python -m debug.session_query SESSION_ID "what is the bid date?"` — full chunk content + LLM reasoning
4. `python -m debug.query "HMA surface course specification"` — all 11 stages with per-chunk context_summary and full content
5. `python -m debug.review --delete SESSION_ID` — confirms deletion
