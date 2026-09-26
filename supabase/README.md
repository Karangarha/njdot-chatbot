# Database migrations

Every schema change to production Supabase (NJ-Dot 2.0) ships as a file in
`supabase/migrations/`. `.github/workflows/supabase-migrations.yml`
applies new files on push to `main`; pull requests get a dry run.

## Writing a migration

1. `supabase migration new <short_name>`. Never invent the filename.
2. One logical change per file. Each file runs in one transaction: if it
   fails, it is rolled back and not recorded, so fix it and push again.
3. **Additive only.** The backend (Azure) and migrations deploy in parallel,
   so either may land first. Add columns, tables and policies. To drop or
   rename something, first ship code that no longer uses it, then the
   migration.
4. Guard statements where cheap: `if not exists`, guarded `create policy`.
5. Never edit a migration once it is on `main`; add a new one. `db push`
   refuses to run if production's history and these files disagree.
6. Preview before merging: `supabase db push --dry-run --linked`, or read
   the dry-run output on the pull request.

## Turning the pipeline on (one time)

The workflow ships disabled. Its job is skipped until the repository
variable `SUPABASE_MIGRATIONS_ENABLED` is `true`. Before production had
this pipeline, migrations 001, 002, 009 and 010 were applied by hand,
and 011 was not (checked 2026-09-26).

1. Add these repository secrets (Settings → Secrets and variables → Actions):
   - `SUPABASE_ACCESS_TOKEN`
   - `SUPABASE_DB_PASSWORD`
   - `SUPABASE_PROJECT_ID` = `rhtbqqprloedsvvdvxak`
2. Link locally, from the repo root:
   `supabase link --project-ref rhtbqqprloedsvvdvxak`
3. Baseline. This marks the migrations production already has, without
   running them, and creates the history table:

   ```bash
   supabase migration repair --status applied 20260101000001 20260101000002 20260101000009 20260101000010 --linked
   ```

4. Check that `supabase db push --dry-run --linked` lists **only**
   `20260101000011_conversations_update_delete_policies.sql`.
5. Set the repository variable `SUPABASE_MIGRATIONS_ENABLED` = `true`
   (Settings → Secrets and variables → Actions → Variables).
6. Run the workflow from the Actions tab on `main`, or merge the next
   migration. It applies 011. Confirm with `supabase migration list --linked`,
   which should show all five versions on both sides.
