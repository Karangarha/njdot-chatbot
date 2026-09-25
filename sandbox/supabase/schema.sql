-- ── NJDOT sandbox: schema top-up for an existing local Supabase ─────────────
--
-- ADDITIVE ONLY. Safe to run against a Supabase that already has the app's
-- schema: every statement is create-if-missing, add-column-if-missing or
-- insert-on-conflict-do-nothing. Nothing is dropped, replaced or rewritten,
-- and no data is touched.
--
-- Covers what the repo has no DDL for (the tables are used by the code but
-- their CREATE statements were never committed — see migration 009's
-- header): review_projects, compliance_checks, session_messages,
-- bdc_section_map, rate_limits, and the public "pdfs" storage bucket
-- app/api/pdf.py streams reference PDFs from. Column lists are derived from
-- every read/write the backend and frontend make.
--
-- Deliberately NOT added: UPDATE/DELETE policies on `conversations`.
-- Production only has what migrations/001 defines (select + insert), and the
-- sandbox must behave like production, so the E2E suite can catch the
-- frontend's conversation delete / updated_at bump being silently
-- filtered by RLS instead of this file papering over it.
--
-- Run with:  ./sandbox.sh db-apply   (applies repo migrations for any
-- missing base objects first, then this file)
-- ─────────────────────────────────────────────────────────────────────────────

create extension if not exists vector;

-- ── review_projects ─────────────────────────────────────────────────────────
create table if not exists public.review_projects (
    id                          uuid primary key default gen_random_uuid(),
    user_id                     uuid not null references auth.users(id) on delete cascade,
    project_name                text not null default 'Untitled Project',
    review_result               jsonb,
    session_id                  text,
    schedule_file_path          text,
    narrative_pdf_path          text,
    special_provision_pdf_path  text,
    key_map_pdf_path            text,
    estimate_pdf_path           text,
    key_map_extraction          jsonb,
    estimate_extraction         jsonb,
    created_at                  timestamptz not null default now(),
    updated_at                  timestamptz not null default now()
);
alter table public.review_projects add column if not exists special_provision_pdf_path text;
alter table public.review_projects add column if not exists key_map_pdf_path text;
alter table public.review_projects add column if not exists estimate_pdf_path text;
alter table public.review_projects add column if not exists key_map_extraction jsonb;
alter table public.review_projects add column if not exists estimate_extraction jsonb;
create index if not exists review_projects_user_created_idx
    on public.review_projects (user_id, created_at desc);

-- ── compliance_checks ───────────────────────────────────────────────────────
-- user_id NULL = shared built-in row (scripts/seed_compliance_checks.py);
-- a user's first edit forks the built-ins into their own rows
-- (frontend/src/lib/checklist.ts ensureForked).
create table if not exists public.compliance_checks (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid references auth.users(id) on delete cascade,
    check_key     text not null,
    category      text not null default 'Custom',
    name          text not null,
    instruction   text not null,
    check_type    text not null default 'llm',
    source_files  jsonb not null default '["schedule"]'::jsonb,
    is_builtin    boolean not null default false,
    enabled       boolean not null default true,
    sort_order    integer not null default 0,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
-- Same indexes as backend/sql/compliance_checks_unique_check_key.sql (the
-- frontend relies on 23505 from these when two tabs fork concurrently).
create unique index if not exists compliance_checks_user_key_uniq
    on public.compliance_checks (user_id, check_key) where user_id is not null;
create unique index if not exists compliance_checks_builtin_key_uniq
    on public.compliance_checks (check_key) where user_id is null;

-- ── session_messages (backend only, service role) ───────────────────────────
create table if not exists public.session_messages (
    id          uuid primary key default gen_random_uuid(),
    session_id  text not null,
    role        text not null,
    content     text not null,
    sources     jsonb not null default '[]'::jsonb,
    created_at  timestamptz not null default now(),
    expires_at  timestamptz not null default (now() + interval '10 years')
);
alter table public.session_messages add column if not exists sources jsonb not null default '[]'::jsonb;
alter table public.session_messages add column if not exists expires_at timestamptz not null default (now() + interval '10 years');
create index if not exists session_messages_session_idx
    on public.session_messages (session_id, created_at);

-- ── bdc_section_map (scripts/ingest_bdc.py, app/retrieval/bdc_matcher.py) ────
create table if not exists public.bdc_section_map (
    id                   uuid primary key default gen_random_uuid(),
    bdc_chunk_id         uuid references public.chunks(id) on delete cascade,
    bdc_id               text not null,
    bdc_date             text,
    effective_date       text,
    implementation_code  text,
    subject              text,
    section_id           text not null,
    section_prefix       text not null,
    change_type          text,
    amendment_text       text
);
create index if not exists bdc_section_map_section_idx on public.bdc_section_map (section_id);

-- ── rate_limits (only scripts/test_db.py reads it, as a smoke test) ─────────
create table if not exists public.rate_limits (
    id            uuid primary key default gen_random_uuid(),
    key           text not null,
    count         integer not null default 0,
    window_start  timestamptz not null default now()
);

-- ── RLS for the tables the browser reads with the anon key + user session ──
alter table public.review_projects   enable row level security;
alter table public.compliance_checks enable row level security;

do $$
begin
    -- review_projects: owner-only CRUD (ChatInterface lists with no user
    -- filter, DocumentReview inserts/updates, sidebar deletes).
    if not exists (select 1 from pg_policies where tablename = 'review_projects' and policyname = 'review_projects_select_own') then
        create policy review_projects_select_own on public.review_projects for select using (auth.uid() = user_id);
    end if;
    if not exists (select 1 from pg_policies where tablename = 'review_projects' and policyname = 'review_projects_insert_own') then
        create policy review_projects_insert_own on public.review_projects for insert with check (auth.uid() = user_id);
    end if;
    if not exists (select 1 from pg_policies where tablename = 'review_projects' and policyname = 'review_projects_update_own') then
        create policy review_projects_update_own on public.review_projects for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
    end if;
    if not exists (select 1 from pg_policies where tablename = 'review_projects' and policyname = 'review_projects_delete_own') then
        create policy review_projects_delete_own on public.review_projects for delete using (auth.uid() = user_id);
    end if;

    -- compliance_checks: everyone reads built-ins; users CRUD their own fork.
    if not exists (select 1 from pg_policies where tablename = 'compliance_checks' and policyname = 'compliance_checks_select') then
        create policy compliance_checks_select on public.compliance_checks for select using (user_id is null or auth.uid() = user_id);
    end if;
    if not exists (select 1 from pg_policies where tablename = 'compliance_checks' and policyname = 'compliance_checks_insert_own') then
        create policy compliance_checks_insert_own on public.compliance_checks for insert with check (auth.uid() = user_id);
    end if;
    if not exists (select 1 from pg_policies where tablename = 'compliance_checks' and policyname = 'compliance_checks_update_own') then
        create policy compliance_checks_update_own on public.compliance_checks for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
    end if;
    if not exists (select 1 from pg_policies where tablename = 'compliance_checks' and policyname = 'compliance_checks_delete_own') then
        create policy compliance_checks_delete_own on public.compliance_checks for delete using (auth.uid() = user_id);
    end if;
end $$;

-- ── Storage buckets ─────────────────────────────────────────────────────────
-- review-files: private, per-user folders (policies live in
-- backend/sql/storage_review_files_policies.sql, applied by db-apply).
insert into storage.buckets (id, name, public)
values ('review-files', 'review-files', false)
on conflict (id) do nothing;

-- pdfs: PUBLIC — app/api/pdf.py fetches
-- {SUPABASE_URL}/storage/v1/object/public/pdfs/<file> with no auth.
insert into storage.buckets (id, name, public)
values ('pdfs', 'pdfs', true)
on conflict (id) do nothing;
