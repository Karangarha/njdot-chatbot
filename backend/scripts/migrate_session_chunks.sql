-- Session-scoped document chunks for the review Q&A feature.
-- Run this once in Supabase SQL Editor before using the session endpoints.

-- ── Table ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS session_chunks (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  TEXT        NOT NULL,
    doc_type    TEXT        NOT NULL,   -- designer_narrative | special_provision | xer_activities
    content     TEXT        NOT NULL,
    embedding   VECTOR(1536),
    metadata    JSONB,
    expires_at  TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '24 hours'
);

-- Fast lookup by session + expiry
CREATE INDEX IF NOT EXISTS idx_session_chunks_session_expires
    ON session_chunks (session_id, expires_at);

-- Vector index (ivfflat; tune lists= based on expected row volume)
CREATE INDEX IF NOT EXISTS idx_session_chunks_embedding
    ON session_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 50);

-- ── Vector search RPC ─────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION match_session_chunks(
    query_embedding  vector(1536),
    p_session_id     text,
    match_count      int     DEFAULT 8,
    match_threshold  float8  DEFAULT 0.2
)
RETURNS TABLE (
    id          uuid,
    content     text,
    doc_type    text,
    metadata    jsonb,
    similarity  float8
)
LANGUAGE sql STABLE
AS $$
    SELECT
        id,
        content,
        doc_type,
        metadata,
        1 - (embedding <=> query_embedding) AS similarity
    FROM  session_chunks
    WHERE session_id = p_session_id
      AND expires_at > now()
      AND 1 - (embedding <=> query_embedding) >= match_threshold
    ORDER BY embedding <=> query_embedding
    LIMIT match_count;
$$;

-- ── Optional: scheduled cleanup (requires pg_cron extension) ──────────────────
-- Uncomment if pg_cron is enabled in your Supabase project.
--
-- SELECT cron.schedule(
--     'cleanup-expired-session-chunks',
--     '0 * * * *',   -- every hour
--     $$DELETE FROM session_chunks WHERE expires_at < now()$$
-- );
