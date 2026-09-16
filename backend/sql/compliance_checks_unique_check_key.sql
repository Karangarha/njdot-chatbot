-- Fix: a user's checklist could be forked twice, giving them 114 rows
-- (57 built-ins x 2) and running every compliance check twice.
--
-- frontend/src/lib/checklist.ts ensureForked() is check-then-act: it asks
-- "do I already own rows?" and, if not, inserts all 57 built-ins. Two edits
-- that overlap in time both pass that question before either insert lands,
-- so both insert the full set. Nothing at the database level said the pair
-- (user_id, check_key) had to be unique, so the second insert was accepted.
--
-- Observed 2026-09-14: one account held 114 rows / 57 distinct check_key,
-- every key exactly twice; the shared built-in template was clean at 57.
--
-- This file is idempotent -- safe to re-run.

-- ── 1. Drop the duplicates, keeping the oldest row per (owner, check_key) ──
-- Both copies are identical: setEnabled/updateCheck filter on
-- (user_id, check_key), so every edit always hit both. Which one survives
-- does not matter; oldest-wins is just deterministic.
delete from compliance_checks
where id in (
  select id
  from (
    select id,
           row_number() over (
             partition by user_id, check_key
             order by created_at asc, id asc
           ) as rn
    from compliance_checks
  ) ranked
  where ranked.rn > 1
);

-- ── 2. Make the duplication impossible from here on ───────────────────────
-- Two partial indexes rather than one unique(user_id, check_key): a plain
-- UNIQUE treats NULLs as distinct, so it would leave the shared built-in
-- rows (user_id IS NULL) unprotected against a double seed.

-- A user owns at most one row per check.
create unique index if not exists compliance_checks_user_key_uniq
  on compliance_checks (user_id, check_key)
  where user_id is not null;

-- The shared built-in template holds at most one row per check.
create unique index if not exists compliance_checks_builtin_key_uniq
  on compliance_checks (check_key)
  where user_id is null;
