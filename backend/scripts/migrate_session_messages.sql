-- Chat message history for session Q&A.
-- Run this once in Supabase SQL Editor (after migrate_session_chunks.sql).

CREATE TABLE IF NOT EXISTS session_messages (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id TEXT        NOT NULL,
    role       TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT        NOT NULL,
    sources    JSONB       NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '24 hours'
);

-- Fast ordered fetch for a session
CREATE INDEX IF NOT EXISTS idx_session_messages_session
    ON session_messages (session_id, expires_at, created_at);

GRANT SELECT, INSERT ON session_messages TO service_role;
