-- 009: Special Provision / Key Map / Estimate become Supabase-only.
-- Run once in the Supabase SQL Editor.
--
-- Adds review_projects.key_map_extraction and .estimate_extraction (jsonb) --
-- the structured LLM extraction for each document, previously persisted only
-- on Neo4j's KeyMapDoc/EstimateDoc.extractionJson. The backend already
-- includes this JSON in every /api/review response (shaped["key_map"]/
-- shaped["estimate"] in backend/app/api/review.py); the frontend starts
-- writing it here on its existing review_projects insert, and the backend
-- reads it back on re-run instead of querying Neo4j.
--
-- The raw chunk text + embeddings for these three documents move to the
-- existing session_chunks table (doc_type IN ('special_provision','key_map',
-- 'estimate')) -- no schema change needed there; doc_type is unconstrained
-- TEXT (see migration 005's note).
--
-- Pre-existing rows keep NULL until the backfill script
-- (scripts/backfill_sp_keymap_estimate_to_supabase.py, Task 6) runs.

ALTER TABLE review_projects
    ADD COLUMN IF NOT EXISTS key_map_extraction  JSONB,
    ADD COLUMN IF NOT EXISTS estimate_extraction JSONB;
