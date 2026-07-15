-- Run once in the Supabase SQL Editor. Creates the private Storage bucket
-- that holds original review uploads (schedule XER, narrative PDF, optional
-- Special Provision PDF), so a review can be re-run later without
-- re-uploading. Path convention: {user_id}/{review_project_id}/<filename>
-- (see migrate_review_projects_add_file_paths.sql for where the paths are
-- recorded).
--
-- The backend's rerun endpoint downloads via the service-role client, which
-- bypasses Storage RLS the same way it bypasses table RLS — no separate
-- grant needed for that path. These policies protect direct
-- browser/anon-key access.

INSERT INTO storage.buckets (id, name, public)
VALUES ('review-files', 'review-files', false)
ON CONFLICT (id) DO NOTHING;

-- Owner-only access, scoped by the first path segment ({user_id}/...) —
-- mirrors this app's established auth.uid() = user_id ownership pattern
-- (see fix_conversations_rls.sql) applied to storage.objects instead of a
-- table. Wrapped in DO blocks since Postgres has no CREATE POLICY IF NOT
-- EXISTS, matching this repo's idempotent-migration convention.
DO $$ BEGIN
    CREATE POLICY "review_files_select_own" ON storage.objects
        FOR SELECT
        USING (bucket_id = 'review-files' AND (storage.foldername(name))[1] = auth.uid()::text);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "review_files_insert_own" ON storage.objects
        FOR INSERT
        WITH CHECK (bucket_id = 'review-files' AND (storage.foldername(name))[1] = auth.uid()::text);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "review_files_update_own" ON storage.objects
        FOR UPDATE
        USING     (bucket_id = 'review-files' AND (storage.foldername(name))[1] = auth.uid()::text)
        WITH CHECK (bucket_id = 'review-files' AND (storage.foldername(name))[1] = auth.uid()::text);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "review_files_delete_own" ON storage.objects
        FOR DELETE
        USING (bucket_id = 'review-files' AND (storage.foldername(name))[1] = auth.uid()::text);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
