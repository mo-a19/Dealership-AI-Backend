create table public.rag_sessions (
  id         text        primary key,
  org_id     uuid        not null references public.organizations(id) on delete cascade,
  user_id    uuid        references public.users(id) on delete set null,
  title      text,                        -- set from the first user message
  history    jsonb       not null default '[]',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index idx_rag_sessions_org_id  on public.rag_sessions(org_id);
create index idx_rag_sessions_user_id on public.rag_sessions(user_id);

create trigger trg_rag_sessions_updated_at
  before update on public.rag_sessions
  for each row execute function public.set_updated_at();

alter table public.rag_sessions enable row level security;

create policy "org_isolation" on public.rag_sessions
  using      (org_id = (auth.jwt() ->> 'org_id')::uuid)
  with check (org_id = (auth.jwt() ->> 'org_id')::uuid);
