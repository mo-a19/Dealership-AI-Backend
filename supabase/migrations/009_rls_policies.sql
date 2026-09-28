-- Enable RLS on every table
alter table public.organizations  enable row level security;
alter table public.users          enable row level security;
alter table public.leads          enable row level security;
alter table public.conversations  enable row level security;
alter table public.messages       enable row level security;
alter table public.kb_documents   enable row level security;
alter table public.kb_chunks      enable row level security;
alter table public.notifications  enable row level security;

-- organizations: its own id is the tenant id
create policy "org_isolation" on public.organizations
  using      (id = (auth.jwt() ->> 'org_id')::uuid)
  with check (id = (auth.jwt() ->> 'org_id')::uuid);

-- All other tables: standard org_id isolation
create policy "org_isolation" on public.users
  using      (org_id = (auth.jwt() ->> 'org_id')::uuid)
  with check (org_id = (auth.jwt() ->> 'org_id')::uuid);

create policy "org_isolation" on public.leads
  using      (org_id = (auth.jwt() ->> 'org_id')::uuid)
  with check (org_id = (auth.jwt() ->> 'org_id')::uuid);

create policy "org_isolation" on public.conversations
  using      (org_id = (auth.jwt() ->> 'org_id')::uuid)
  with check (org_id = (auth.jwt() ->> 'org_id')::uuid);

create policy "org_isolation" on public.messages
  using      (org_id = (auth.jwt() ->> 'org_id')::uuid)
  with check (org_id = (auth.jwt() ->> 'org_id')::uuid);

create policy "org_isolation" on public.kb_documents
  using      (org_id = (auth.jwt() ->> 'org_id')::uuid)
  with check (org_id = (auth.jwt() ->> 'org_id')::uuid);

create policy "org_isolation" on public.kb_chunks
  using      (org_id = (auth.jwt() ->> 'org_id')::uuid)
  with check (org_id = (auth.jwt() ->> 'org_id')::uuid);

create policy "org_isolation" on public.notifications
  using      (org_id = (auth.jwt() ->> 'org_id')::uuid)
  with check (org_id = (auth.jwt() ->> 'org_id')::uuid);
