# Automated Supabase Migrations on GitHub Push: Design

**Date:** 2026-09-26
**Status:** Approved in conversation (Sections 1–3). Awaiting review of this written spec.

## Goal

When a migration file reaches `main`, it is applied to production Supabase (**NJ-Dot 2.0**, project ref `rhtbqqprloedsvvdvxak`) automatically. Nobody pastes SQL into the SQL Editor any more.

Success means:
- Production always matches the repo's migrations.
- Each migration runs exactly once.
- A bad migration fails the pipeline without half-applying.

## Context: how things work today (checked 2026-09-26)

**Where migrations live:**
- `backend/migrations/` holds numbered files: `001`, `002`, `009`, `010`, `011`. Numbers 003–008 were never committed.
- These files are not in Supabase CLI format.

**How they reach production:** they're pasted in by hand. Production has no `supabase_migrations.schema_migrations` table.

**What production has:** a read-only check found every object from 001, 002, 009 and 010.
- The two 010 functions match the local database exactly (same signature, same body md5).
- **011 is missing:** `conversations` has no UPDATE or DELETE policy, so deleting a chat and the `updated_at` bump both silently do nothing in production. The `messages` ON DELETE CASCADE that 011 also restores already exists there.

**Other deploys:**
- The backend deploys to Azure through `.github/workflows/azure-deploy-backend.yml`, on push to `main` touching `backend/**`.
- The frontend deploys through Vercel.
- The SQL function files in `backend/sql/` are applied separately by `backend/scripts/deploy_sql.py`.

## Decisions (made by the user)

1. **Auto-apply on merge to `main`.** Pull requests get a dry run that lists pending migrations and applies nothing.
2. **Supabase CLI `db push`.** Not a custom runner, and not re-running every file.
3. **A GitHub Action, not the Supabase GitHub integration, for now.**
   - The integration builds each PR's preview branch from the migrations alone. That fails today, because migration 009 alters `review_projects` and no migration creates it (plan Task 7).
   - Preview branches also bill per hour.
   - Revisit once Task 7's baseline migration makes the migrations able to build a fresh database. Switching then means deleting the workflow and connecting the repo in the dashboard, since both use the same folder and the same history table.

4. **Amended 2026-09-26: connect with a connection string, not `supabase link`.**
   - `link` needs a personal access token, and a scoped token is capped by its owner's organization role. The real token first failed on `project_admin_read`, then on `api_gateway_keys_read`, and then because the owning account's role couldn't read API keys.
   - The workflow now runs `supabase db push [--dry-run] --db-url "$SUPABASE_DB_URL"`, using the Session pooler string, because GitHub runners are IPv4-only.
   - This replaces the three secrets in Section 2 with the single secret `SUPABASE_DB_URL`, scoped to the two push steps.
   - The baseline and checks in `supabase/README.md` use `--db-url` too.

## Verified CLI behavior (CLI 2.109.0, against a throwaway Postgres container)

| Question | Result |
|---|---|
| Does `migration repair --status applied <v>` work on a database with no history table? | Yes. It creates `supabase_migrations.schema_migrations` and records the version without running the file. |
| What does `db push --dry-run` list? | Only versions missing from history. Baselined versions are skipped, and nothing is applied. |
| A migration file fails partway through. What remains? | Nothing. The statements before the error are rolled back and no history row is written, so each file is atomic. The push stops at the failing file. |
| Can `db push` reach a plain local Postgres? | Only with `PGSSLMODE=disable` in the environment. The `sslmode` parameter in `--db-url` is ignored. This applies to local testing only; hosted Supabase uses TLS. |

## Design

### 1. File layout and the one-time baseline

- **Project folder:** add `supabase/` at the repo root, containing `config.toml` (from `supabase init`) and `migrations/`.
- **Move the existing migrations with `git mv`** so history is kept. The new names use the CLI's `<timestamp>_<name>.sql` format, with the old number kept in the version:

  | Before | After |
  |---|---|
  | `backend/migrations/001_conversations.sql` | `supabase/migrations/20260101000001_conversations.sql` |
  | `backend/migrations/002_conversations_updated_at.sql` | `supabase/migrations/20260101000002_conversations_updated_at.sql` |
  | `backend/migrations/009_review_projects_sp_keymap_estimate_supabase_only.sql` | `supabase/migrations/20260101000009_review_projects_sp_keymap_estimate.sql` |
  | `backend/migrations/010_session_chunks_hybrid.sql` | `supabase/migrations/20260101000010_session_chunks_hybrid.sql` |
  | `backend/migrations/011_conversations_update_delete_policies.sql` | `supabase/migrations/20260101000011_conversations_update_delete_policies.sql` |

- **New migrations** are created with `supabase migration new <name>`, never with invented names.
- **Baseline production once:** `supabase migration repair --status applied 20260101000001 20260101000002 20260101000009 20260101000010 --linked`.
  - This writes production's history table. It's a production write, so it needs the user's explicit OK.
  - 011 is deliberately left unmarked, so **the pipeline's first run applies 011 to production.**
- **Update the references to the old path:**
  - `sandbox/sandbox.sh` `cmd_db_apply`: the 001/002 bootstrap lines and the numbered-migration loop move to `supabase/migrations/`. The loop's `>= 011` filter becomes "version ≥ 20260101000011".
  - `backend/.gitignore`: the `!migrations/` rules become unnecessary. Leave them; they're harmless.
  - Docs that name `backend/migrations/` as the live location: README and `sandbox/FINDINGS.md`. Historical plan docs keep their text.
- **Out of scope:**
  - `backend/sql/` and `deploy_sql.py` are unchanged. Future function changes ship as new migrations.
  - Task 7's missing-tables DDL becomes a new migration in `supabase/migrations/` when it's written.

### 2. The GitHub Actions workflow

New file: `.github/workflows/supabase-migrations.yml`.

- **Triggers:**
  - `push` to `main`, with paths `supabase/migrations/**` and the workflow file.
  - `pull_request`, same paths.
  - `workflow_dispatch`.
- **Steps:**
  1. `actions/checkout`.
  2. `supabase/setup-cli@v1` with `version: 2.109.0`, pinned rather than `latest` so a CLI release can't change behavior.
  3. `supabase link --project-ref "$SUPABASE_PROJECT_ID"`.
  4. On a pull request: `supabase db push --dry-run`. Otherwise: `supabase db push`.
- **Concurrency:** `group: supabase-migrations-production`, `cancel-in-progress: false`, so pushes queue instead of racing.
- **Environment:** set from repository secrets that the user adds; Claude never handles the values.
  - `SUPABASE_ACCESS_TOKEN`: a personal access token.
  - `SUPABASE_DB_PASSWORD`: NJ-Dot 2.0's database password.
  - `SUPABASE_PROJECT_ID`: `rhtbqqprloedsvvdvxak`.
- **Built-in safety:** if production's history contains a version with no matching local file (an applied migration was edited or deleted), `db push` refuses to run, and the job fails without applying anything.

### 3. Deploy ordering, rules for writing migrations, and verification

**Ordering:** the Azure backend deploy and this workflow run in parallel with no dependency between them. Instead of chaining workflows, one rule: **migrations must be additive and backward compatible**, so either order works. A destructive change takes two pushes: first the code stops using the object, then a migration drops it.

**`supabase/README.md`** documents the rules:
- Create files with `supabase migration new <name>`.
- Never edit a migration that's already on `main`; add a new one.
- One logical change per file. Files are atomic, so a failure is clean and easy to re-run.
- Guard statements with `if not exists` where it's cheap.
- Additive only (the ordering rule above).
- How to preview locally with `supabase db push --dry-run --linked`.

**Verification:**
1. **Local, throwaway database:** show that the renamed files baseline and push the same way the experiment above did.
2. **Production write, needs the user's OK:** run the baseline `migration repair`. Then `supabase migration list --linked` shows 001, 002, 009 and 010 as applied remotely and 011 as local-only.
3. **Production, read-only, after step 2:** `supabase db push --dry-run --linked` must list **only** `20260101000011`. Before the baseline exists, every version would show as pending.
4. **First real run:** after the merge to `main`, the Action applies 011. Re-run the read-only production check: `conversations` must now have UPDATE and DELETE policies, and `migration list --linked` must show all 5 versions.

## Risks

- **An edited or deleted applied migration** makes `db push` refuse to run. That's the intended guard; fix it with `migration repair`, not by editing history by hand.
- **A leaked database password or access token** would expose production. Both live only in GitHub secrets. The access token can be scoped and rotated in the Supabase dashboard.
- **A non-additive migration merged alongside code that still uses the object** can break the running backend for as long as the two deploys race. Mitigated by the additive rule and PR review of the dry-run output.
