-- Migration 002: add updated_at to conversations for recency ordering
ALTER TABLE conversations
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Drop old index if exists, create better one ordered by updated_at
DROP INDEX IF EXISTS conversations_user_id_idx;
CREATE INDEX IF NOT EXISTS conversations_user_updated_idx
  ON conversations (user_id, updated_at DESC);
