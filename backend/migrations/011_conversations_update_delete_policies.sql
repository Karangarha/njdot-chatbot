-- 011: conversations need UPDATE and DELETE policies.
-- 001 only granted SELECT/INSERT, so the frontend's delete
-- (ChatInterface.tsx handleDeleteConversation) and its updated_at bump
-- after each message matched 0 rows under RLS and silently did nothing.
-- Messages go with their conversation via ON DELETE CASCADE, so messages
-- needs no DELETE policy. Idempotent: safe to re-run on live.
--
-- Some databases (e.g. one built before 001 was committed) have
-- messages.conversation_id WITHOUT the cascade 001 declares, so the delete
-- fails with 409 (FK violation) instead. Restore the cascade where missing.
do $$
declare
  fk record;
  fixed boolean := false;
begin
  for fk in select conname from pg_constraint
            where conrelid = 'public.messages'::regclass
              and confrelid = 'public.conversations'::regclass
              and contype = 'f' and confdeltype <> 'c'
  loop
    execute format('alter table public.messages drop constraint %I', fk.conname);
    fixed := true;
  end loop;
  if fixed then
    alter table public.messages
      add constraint messages_conversation_id_fkey
      foreign key (conversation_id) references public.conversations(id) on delete cascade;
  end if;

  if not exists (select 1 from pg_policies
                 where schemaname = 'public' and tablename = 'conversations'
                   and policyname = 'conversations_update') then
    create policy "conversations_update" on public.conversations
      for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
  end if;
  if not exists (select 1 from pg_policies
                 where schemaname = 'public' and tablename = 'conversations'
                   and policyname = 'conversations_delete') then
    create policy "conversations_delete" on public.conversations
      for delete using (auth.uid() = user_id);
  end if;
end $$;
