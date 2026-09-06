-- Storage RLS for the "review-files" bucket, needed for the frontend to
-- upload review documents (schedule XER, narrative/SP/key map/estimate PDFs)
-- directly to Storage from the browser, instead of routing the raw bytes
-- through POST /api/review -- which was hitting Vercel's fixed 4.5MB
-- serverless request-body limit on real projects (a real Special Provisions
-- PDF alone is often 50-100+ pages), surfacing to the browser as a
-- misleading CORS error rather than the actual 413 FUNCTION_PAYLOAD_TOO_LARGE.
--
-- Every object this app writes under this bucket is keyed
-- "<user_id>/<project_id>/<filename>" (see backend/app/api/review.py's
-- `base = f"{user_id}/{project_id}"` and the frontend's matching upload
-- path in DocumentReview.tsx) -- policies check that the first path segment
-- is the caller's own auth.uid().
--
-- The backend's own Storage access (uploads on the raw-upload path,
-- downloads for re-run and PDF-serving) uses the service-role key, which
-- bypasses RLS entirely -- these policies only govern direct client access.

-- Create the bucket if it doesn't already exist (private -- no public policy
-- is added below, so objects are only reachable via an authenticated
-- request that matches one of these policies, or the service role).
insert into storage.buckets (id, name, public)
values ('review-files', 'review-files', false)
on conflict (id) do nothing;

-- Signed-in users can upload into their own folder.
drop policy if exists "review_files_insert_own" on storage.objects;
create policy "review_files_insert_own"
  on storage.objects for insert
  with check (
    bucket_id = 'review-files'
    and auth.uid()::text = (storage.foldername(name))[1]
  );

-- Signed-in users can overwrite their own objects (upload(..., {upsert:
-- true}) issues an UPDATE when the object already exists).
drop policy if exists "review_files_update_own" on storage.objects;
create policy "review_files_update_own"
  on storage.objects for update
  using (
    bucket_id = 'review-files'
    and auth.uid()::text = (storage.foldername(name))[1]
  )
  with check (
    bucket_id = 'review-files'
    and auth.uid()::text = (storage.foldername(name))[1]
  );

-- Signed-in users can read back their own uploads (not required by the
-- current upload-then-let-the-backend-download flow, but consistent with
-- the insert/update policies and harmless to have).
drop policy if exists "review_files_select_own" on storage.objects;
create policy "review_files_select_own"
  on storage.objects for select
  using (
    bucket_id = 'review-files'
    and auth.uid()::text = (storage.foldername(name))[1]
  );
