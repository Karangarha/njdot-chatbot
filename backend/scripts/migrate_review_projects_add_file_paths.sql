-- Run once in the Supabase SQL Editor. Adds nullable columns to
-- review_projects pointing at the original uploaded files in the
-- 'review-files' Storage bucket (see migrate_review_files_storage.sql),
-- so a review can be re-run later without re-uploading.
--
-- Existing rows (reviews saved before this feature) simply have NULL paths
-- — the frontend's "Re-run Review" action should be hidden/disabled for
-- those until the user re-uploads once.

ALTER TABLE review_projects
    ADD COLUMN IF NOT EXISTS schedule_file_path         TEXT,
    ADD COLUMN IF NOT EXISTS narrative_pdf_path          TEXT,
    ADD COLUMN IF NOT EXISTS special_provision_pdf_path  TEXT;
