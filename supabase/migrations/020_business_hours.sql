alter table public.organizations
  add column if not exists business_hours jsonb not null default '{}';
