-- Session-scoped knowledge graphs (schedule + narrative) for GraphRAG Q&A.
-- Run this once in Supabase SQL Editor before using the graph features.
--
-- The graph column holds networkx node_link_data JSON: {nodes: [...], links: [...]}.
-- Traversal happens in-process (Python/networkx); this table is storage only.

CREATE TABLE IF NOT EXISTS session_graphs (
    session_id   TEXT        PRIMARY KEY,
    graph        JSONB       NOT NULL,
    cpm_summary  JSONB       NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '24 hours'
);

CREATE INDEX IF NOT EXISTS idx_session_graphs_expires
    ON session_graphs (expires_at);

-- The backend accesses this table with the service-role key only.
GRANT SELECT, INSERT, UPDATE, DELETE ON public.session_graphs TO service_role;

-- ── Optional: scheduled cleanup (requires pg_cron extension) ──────────────────
-- Uncomment if pg_cron is enabled in your Supabase project.
--
-- SELECT cron.schedule(
--     'cleanup-expired-session-graphs',
--     '0 * * * *',   -- every hour
--     $$DELETE FROM session_graphs WHERE expires_at < now()$$
-- );
