-- Testing/pre-production: make Q&A sessions persist until explicitly deleted.
--
-- The session tables were created with a 24-hour expires_at TTL. This script
-- (idempotent, safe to re-run) pushes the default AND all existing rows out to
-- 10 years, so sessions live until removed via DELETE /api/session/{id} or the
-- Supabase dashboard. It also ensures service_role has full table privileges
-- (needed by the delete endpoint; avoids 42501 like compliance_checks hit).
--
-- Run AFTER the base migrations (migrate_session_chunks / _graphs / _messages).
-- To restore production behaviour: set the defaults back to '24 hours' and
-- enable the pg_cron cleanup jobs in the base migration files.

-- ── Defaults for new rows ─────────────────────────────────────────────────────
ALTER TABLE session_chunks   ALTER COLUMN expires_at SET DEFAULT now() + INTERVAL '10 years';
ALTER TABLE session_graphs   ALTER COLUMN expires_at SET DEFAULT now() + INTERVAL '10 years';
ALTER TABLE session_messages ALTER COLUMN expires_at SET DEFAULT now() + INTERVAL '10 years';

-- ── Revive/extend existing rows (incl. already-expired ones) ─────────────────
UPDATE session_chunks   SET expires_at = now() + INTERVAL '10 years';
UPDATE session_graphs   SET expires_at = now() + INTERVAL '10 years';
UPDATE session_messages SET expires_at = now() + INTERVAL '10 years';

-- ── Privileges for the backend's service-role client ─────────────────────────
GRANT ALL ON session_chunks   TO service_role;
GRANT ALL ON session_graphs   TO service_role;
GRANT ALL ON session_messages TO service_role;
