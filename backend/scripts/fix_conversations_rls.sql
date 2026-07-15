-- Fix: `conversations` and `messages` had RLS enabled with ZERO policies on
-- the local Supabase instance (table + RLS flag existed, but no CREATE POLICY
-- ever ran locally — likely only ever applied directly against production and
-- never captured in a tracked migration). With RLS on and no policies, every
-- request is denied regardless of grants, which is what produced the 403 on
-- GET /rest/v1/conversations from ChatInterface.tsx's loadConversations().
--
-- Ownership matches the frontend's existing usage in
-- frontend/src/components/chat/ChatInterface.tsx: conversations are scoped
-- by `user_id = auth.uid()`; messages have no user_id column of their own,
-- so they're scoped via their parent conversation's ownership.
--
-- Safe to run against production too — CREATE POLICY IF NOT EXISTS guards
-- re-runs, and this only ADDS policies where none exist; it does not touch
-- any policy that might already be present elsewhere.

-- ── conversations ────────────────────────────────────────────────────────────
DO $$ BEGIN
    CREATE POLICY "users_own_conversations_select" ON conversations
        FOR SELECT USING (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "users_own_conversations_insert" ON conversations
        FOR INSERT WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "users_own_conversations_update" ON conversations
        FOR UPDATE USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "users_own_conversations_delete" ON conversations
        FOR DELETE USING (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON conversations TO authenticated;

-- ── messages (ownership via parent conversation) ────────────────────────────
DO $$ BEGIN
    CREATE POLICY "users_own_conversation_messages_select" ON messages
        FOR SELECT USING (
            conversation_id IN (SELECT id FROM conversations WHERE user_id = auth.uid())
        );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "users_own_conversation_messages_insert" ON messages
        FOR INSERT WITH CHECK (
            conversation_id IN (SELECT id FROM conversations WHERE user_id = auth.uid())
        );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

GRANT SELECT, INSERT ON messages TO authenticated;
