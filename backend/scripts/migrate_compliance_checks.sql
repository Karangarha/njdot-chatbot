-- Run this once in the Supabase SQL Editor to enable the configurable
-- compliance checklist.  Built-in checks live as rows with user_id IS NULL
-- (seeded by scripts/seed_compliance_checks.py); each user forks their own
-- editable copies (copy-on-write) with user_id = their auth.uid().

CREATE TABLE IF NOT EXISTS compliance_checks (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID,                                    -- NULL = built-in/global template
    check_key   TEXT        NOT NULL,
    category    TEXT        NOT NULL,
    name        TEXT        NOT NULL,
    instruction TEXT        NOT NULL DEFAULT '',
    check_type  TEXT        NOT NULL DEFAULT 'llm',       -- descriptive only; always 'llm'
    source_files JSONB      NOT NULL DEFAULT '["schedule"]'::jsonb,  -- array of "schedule"|"narrative"|"sp"
    is_builtin  BOOLEAN     NOT NULL DEFAULT FALSE,
    enabled     BOOLEAN     NOT NULL DEFAULT TRUE,        -- drives "run only selected"
    sort_order  INTEGER     NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Safe to re-run if the table already existed before source_files was added.
ALTER TABLE compliance_checks
    ADD COLUMN IF NOT EXISTS source_files JSONB NOT NULL DEFAULT '["schedule"]'::jsonb;

-- One built-in row per check_key; one user row per (user, check_key).
CREATE UNIQUE INDEX IF NOT EXISTS ux_compliance_builtin_key
    ON compliance_checks (check_key) WHERE user_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_compliance_user_key
    ON compliance_checks (user_id, check_key) WHERE user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_compliance_user
    ON compliance_checks (user_id, sort_order);

ALTER TABLE compliance_checks ENABLE ROW LEVEL SECURITY;

-- Anyone signed in may READ their own rows and the shared built-ins.
CREATE POLICY "read_own_and_builtin" ON compliance_checks
    FOR SELECT
    USING (user_id = auth.uid() OR user_id IS NULL);

-- Users may only WRITE their own rows.  Built-in rows (user_id IS NULL) are
-- therefore read-only to end users and only editable via the service role.
CREATE POLICY "insert_own" ON compliance_checks
    FOR INSERT
    WITH CHECK (user_id = auth.uid());

CREATE POLICY "update_own" ON compliance_checks
    FOR UPDATE
    USING     (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

CREATE POLICY "delete_own" ON compliance_checks
    FOR DELETE
    USING (user_id = auth.uid());

GRANT ALL ON compliance_checks TO authenticated;

-- The seed script (scripts/seed_compliance_checks.py) connects as service_role to
-- (re)write the built-in rows.  service_role bypasses RLS but still needs table-
-- level privileges, which are not granted by default on a freshly created table.
GRANT ALL ON compliance_checks TO service_role;
