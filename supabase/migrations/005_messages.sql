-- Append-only: no updated_at, no update trigger
create table public.messages (
  id              uuid        primary key default gen_random_uuid(),
  conversation_id uuid        not null references public.conversations(id) on delete cascade,
  org_id          uuid        not null references public.organizations(id) on delete cascade,
  role            text        not null check (role in ('user', 'assistant', 'agent')),
  content         text        not null,
  sources_used    jsonb,
  tokens_used     integer,
  -- Wasender message key.id — prevents double-insert on retried webhooks
  wa_message_id   text,
  created_at      timestamptz not null default now(),
  unique (org_id, wa_message_id)
);

create index idx_messages_conversation_id on public.messages(conversation_id);
create index idx_messages_org_id          on public.messages(org_id);
