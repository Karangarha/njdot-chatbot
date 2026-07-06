# Debug Scripts — Usage Guide

All commands are run from the `backend/` directory using the venv Python:

```
cd backend/
Scripts/python.exe -m debug.<script> [args]
```

---

## Script 1: `debug.review` — Compliance Review + Session Ingestion

### Run a full compliance review and create a RAG session

```
Scripts/python.exe -m debug.review schedule.xer narrative.pdf
Scripts/python.exe -m debug.review schedule.xer narrative.pdf --sp special_provision.pdf
Scripts/python.exe -m debug.review schedule.xer narrative.pdf --sp sp.pdf --show-prompt
```

The `--sp` flag is optional. If provided, the special provision PDF is chunked and added to the session alongside the narrative and XER data.

**What you see:**

| Stage | Output |
|-------|--------|
| 1 — File reading | XER, narrative, and SP file sizes; SP shown as "not provided" if omitted |
| 2 — XER parsing | Activity counts, WBS phases, negative-float list, first 5 activities (full JSON), all milestones (full JSON) |
| 3 — Prompt assembly | JSON string length, PDF base64 length; full system + user prompts with `--show-prompt` |
| 4 — LLM call | Model used (GPT-4o or Claude fallback), elapsed time |
| 5 — Raw LLM response | Complete unedited JSON string from the model |
| 6 — Per-check results | All 30 checks, each showing: `STATUS` (✓/✗/⚠), `REASONING` (full multi-line), `FINDING`, `EVIDENCE` |
| 7 — Summary | Passed / Warnings / Failed / Manual review counts |
| 8a — Narrative chunking | Page count, each chunk with heading + full content |
| 8b — XER chunking | Each milestone chunk and phase-summary chunk with full content |
| 8c — SP chunking | Page count, each sliding-window chunk with page number + full content; skipped if `--sp` not given |
| 8d — Embedding | Per-batch progress, model name, dimensions |
| 8e — Supabase insert | Row insert progress |

After the insert, the session ID is printed in a prominent banner **twice** — once at the start of Stage 8 and again at the end — so it's easy to copy:

```
████████████████████████████████████████████████████████████████████████████████
SESSION ID:  a1b2c3d4-e5f6-7890-abcd-ef1234567890
████████████████████████████████████████████████████████████████████████████████

To query this session:
  Scripts/python.exe -m debug.session_query a1b2c3d4-... "your question"
```

---

### List saved sessions

```
Scripts/python.exe -m debug.review --list
```

Shows all sessions from `sessions.json` with live chunk counts queried from Supabase.

---

### Delete a session

```
Scripts/python.exe -m debug.review --delete SESSION_ID
```

Removes all `session_chunks` rows for this session from Supabase and updates `sessions.json`.

---

## Script 2: `debug.session_query` — Debug Session Q&A

Query a session's RAG chunks (created by `debug.review`) with full intermediate output.

```
Scripts/python.exe -m debug.session_query SESSION_ID "your question"
Scripts/python.exe -m debug.session_query SESSION_ID "your question" --match-count 12
Scripts/python.exe -m debug.session_query SESSION_ID "your question" --show-prompt
```

**What you see:**

| Stage | Output |
|-------|--------|
| 1 — Session check | Total chunk count + breakdown by `doc_type` (xer_activities, designer_narrative, special_provision) |
| 2 — Question embedding | Embedding model, dimension, first 10 vector values |
| 3 — Session chunk search | Full content + complete metadata for every retrieved chunk; similarity score |
| 4 — Scheduling manual search | Chunks from the permanent scheduling collection, including `context_summary` |
| 5 — Prompt assembly | Context character count; full prompts with `--show-prompt` |
| 6 — LLM call | Provider, model, elapsed time |
| 7 — Raw LLM response | Complete unedited response text |
| 8 — Final answer + sources | Clean answer, source list with label / heading / page / similarity |

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--match-count N` | 8 | Number of session chunks to retrieve |
| `--show-prompt` | off | Print full system and user prompts |

---

## Script 3: `debug.query` — Debug Main RAG Pipeline

Query the permanent NJDOT spec/scheduling/material collections with full intermediate output.

```
Scripts/python.exe -m debug.query "your question"
Scripts/python.exe -m debug.query "your question" --collection specs_2019
Scripts/python.exe -m debug.query "your question" --show-prompt
```

**What you see:**

| Stage | Output |
|-------|--------|
| 1 — Query classification | Type (semantic / mixed / keyword-heavy), vector weight, keyword weight |
| 2 — Query expansion | Full expansion system prompt, user message with few-shot examples, 3 query variants (original, spec_style, keywords) |
| 3 — Per-variant search | For each of the 3 variants: all chunks with `vector_rank`, `keyword_rank`, `rrf_score`, `context_summary`, and **full content** |
| 4 — Multi-RRF merge | Candidate pool size, section dedup results, final 12 chunks with `query_count` (how many variants found each) |
| 5 — Cross-reference follow-up | Section numbers found in chunk text, which were new vs. already retrieved, full content of cross-ref chunks |
| 6 — BDC amendments | All amendments matching retrieved sections: `bdc_id`, change type, implementation code, full amendment text |
| 7 — Prompt assembly | Chunk and amendment counts, prompt lengths; full prompts with `--show-prompt` |
| 8 — LLM call | Provider, model, elapsed time |
| 9 — Raw LLM response | Complete unedited JSON from the model |
| 10 — Citation validation | For each citation: match method (chunk_id / section_id), verified status, page number corrections |
| 11 — Final answer | Clean answer text + validated citations with document / section / page / chunk_id |

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--collection` | all | Restrict to `specs_2019`, `scheduling`, or `material_procs` |
| `--show-prompt` | off | Print full system and user prompts (can be very long) |

---

## Typical Workflow

```bash
# 1. Run a compliance review — also creates a RAG session
#    (add --sp special_provision.pdf if you have one)
Scripts/python.exe -m debug.review path/to/schedule.xer path/to/narrative.pdf
Scripts/python.exe -m debug.review path/to/schedule.xer path/to/narrative.pdf --sp path/to/sp.pdf

# 2. Copy the SESSION ID printed in the banner, or list sessions
Scripts/python.exe -m debug.review --list

# 3. Ask questions against that session with full debug output
Scripts/python.exe -m debug.session_query <SESSION_ID> "what is the bid date?"
Scripts/python.exe -m debug.session_query <SESSION_ID> "are there any winter paving conflicts?"
Scripts/python.exe -m debug.session_query <SESSION_ID> "what does the SP say about traffic control?" --match-count 12

# 4. Query the main NJDOT RAG for spec questions
Scripts/python.exe -m debug.query "what is the HMA surface course specification?"
Scripts/python.exe -m debug.query "section 902.02 mix design requirements" --collection specs_2019

# 5. Clean up when done
Scripts/python.exe -m debug.review --delete <SESSION_ID>
```

---

## Session State

Sessions are saved to `backend/debug/sessions.json`. Each entry records:

```json
{
  "session_id": "uuid",
  "created_at": "2026-07-06T12:34:56",
  "xer_file": "schedule.xer",
  "narrative_file": "narrative.pdf",
  "sp_file": "special_provision.pdf",
  "project_name": "I-95 Interchange",
  "chunk_count": 89
}
```

`sp_file` is `null` if no special provision was provided. Sessions persist in Supabase (`session_chunks` table) until explicitly deleted with `--delete`.
