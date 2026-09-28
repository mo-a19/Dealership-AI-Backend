create table public.notifications (
  id         uuid        primary key default gen_random_uuid(),
  org_id     uuid        not null references public.organizations(id) on delete cascade,
  user_id    uuid        references public.users(id) on delete set null,
  lead_id    uuid        references public.leads(id) on delete set null,
  type       text        not null,
  channel    text        not null,
  is_read    boolean     not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index idx_notifications_org_id  on public.notifications(org_id);
create index idx_notifications_user_id on public.notifications(user_id);
create index idx_notifications_lead_id on public.notifications(lead_id);

create trigger trg_notifications_updated_at
  before update on public.notifications
  for each row execute function public.set_updated_at();
