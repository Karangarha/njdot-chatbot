-- Run this once in the Supabase SQL Editor to enable persistent review projects.

CREATE TABLE IF NOT EXISTS review_projects (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID        NOT NULL,
    session_id    TEXT,
    project_name  TEXT        NOT NULL DEFAULT 'Untitled Project',
    review_result JSONB       NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE review_projects ENABLE ROW LEVEL SECURITY;

CREATE POLICY "users_own_projects" ON review_projects
    FOR ALL
    USING     (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE INDEX IF NOT EXISTS idx_review_projects_user
    ON review_projects (user_id, created_at DESC);

GRANT ALL ON review_projects TO authenticated;
