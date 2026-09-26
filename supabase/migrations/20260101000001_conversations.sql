-- ──────────────────────────────────────────────────────────────────────────────
-- Migration: 001_conversations
-- Run this in the Supabase SQL Editor (Dashboard → SQL Editor → New query).
-- ──────────────────────────────────────────────────────────────────────────────

-- conversations ----------------------------------------------------------------

create table if not exists conversations (
  id         uuid        primary key default gen_random_uuid(),
  user_id    uuid        not null references auth.users(id) on delete cascade,
  title      text        not null,
  created_at timestamptz not null default now()
);

create index if not exists conversations_user_id_idx
  on conversations (user_id, created_at desc);

-- messages ---------------------------------------------------------------------

create table if not exists messages (
  id               uuid        primary key default gen_random_uuid(),
  conversation_id  uuid        not null references conversations(id) on delete cascade,
  role             text        not null check (role in ('user', 'assistant')),
  content          text        not null,
  citations        jsonb       not null default '[]',
  bdc_alerts       jsonb       not null default '[]',
  created_at       timestamptz not null default now()
);

create index if not exists messages_conversation_id_idx
  on messages (conversation_id, created_at);

-- Row Level Security -----------------------------------------------------------

alter table conversations enable row level security;
alter table messages       enable row level security;

-- Conversations: users can only see and create their own rows
create policy "conversations_select" on conversations
  for select using ( auth.uid() = user_id );

create policy "conversations_insert" on conversations
  for insert with check ( auth.uid() = user_id );

-- Messages: users can see / insert messages for their own conversations
create policy "messages_select" on messages
  for select using (
    exists (
      select 1 from conversations c
      where c.id = messages.conversation_id
        and c.user_id = auth.uid()
    )
  );

create policy "messages_insert" on messages
  for insert with check (
    exists (
      select 1 from conversations c
      where c.id = messages.conversation_id
        and c.user_id = auth.uid()
    )
  );
