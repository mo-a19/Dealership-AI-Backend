create table public.conversations (
  id           uuid        primary key default gen_random_uuid(),
  org_id       uuid        not null references public.organizations(id) on delete cascade,
  lead_id      uuid        not null references public.leads(id) on delete cascade,
  mode         text        not null default 'ai' check (mode in ('ai', 'human')),
  assigned_to  uuid        references public.users(id) on delete set null,
  status       text        not null default 'open',
  -- WhatsApp chatId (remoteJid e.g. 1234567890@s.whatsapp.net)
  wa_thread_id text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  -- one WhatsApp chat = one conversation per tenant
  unique (org_id, wa_thread_id)
);

create index idx_conversations_org_id      on public.conversations(org_id);
create index idx_conversations_lead_id     on public.conversations(lead_id);
create index idx_conversations_assigned_to on public.conversations(assigned_to);

create trigger trg_conversations_updated_at
  before update on public.conversations
  for each row execute function public.set_updated_at();
