-- 1:1 with auth.users; id is the same UUID Supabase Auth assigns
create table public.users (
  id         uuid        primary key references auth.users(id) on delete cascade,
  org_id     uuid        not null references public.organizations(id) on delete cascade,
  email      text        not null,
  full_name  text,
  role       text        not null check (role in ('admin', 'agent', 'viewer')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index idx_users_org_id on public.users(org_id);

create trigger trg_users_updated_at
  before update on public.users
  for each row execute function public.set_updated_at();
