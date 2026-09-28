create table public.leads (
  id          uuid        primary key default gen_random_uuid(),
  org_id      uuid        not null references public.organizations(id) on delete cascade,
  assigned_to uuid        references public.users(id) on delete set null,
  name        text,
  phone       text        not null,
  channel     text,
  status      text        not null default 'new'
                check (status in ('new', 'hot', 'warm', 'cold', 'converted')),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index  idx_leads_org_id      on public.leads(org_id);
create index  idx_leads_assigned_to on public.leads(assigned_to);
-- find-or-create by WhatsApp phone number within a tenant
create unique index idx_leads_org_phone on public.leads(org_id, phone);

create trigger trg_leads_updated_at
  before update on public.leads
  for each row execute function public.set_updated_at();
