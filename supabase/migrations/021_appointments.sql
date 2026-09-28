create type public.appointment_type as enum (
  'test_drive', 'trade_in', 'maintenance', 'apk', 'damage_repair', 'inquiry'
);

create type public.appointment_status as enum (
  'pending', 'confirmed', 'completed', 'cancelled'
);

create table public.appointments (
  id                uuid                     primary key default gen_random_uuid(),
  org_id            uuid                     not null references public.organizations(id) on delete cascade,
  lead_id           uuid                     references public.leads(id) on delete set null,
  type              public.appointment_type  not null,
  scheduled_at      timestamptz              not null,
  status            public.appointment_status not null default 'pending',
  notes             text,
  reminder_sent_24h boolean                  not null default false,
  reminder_sent_1h  boolean                  not null default false,
  created_at        timestamptz              not null default now(),
  updated_at        timestamptz              not null default now()
);

create index idx_appointments_org_id       on public.appointments(org_id);
create index idx_appointments_lead_id      on public.appointments(lead_id);
create index idx_appointments_scheduled_at on public.appointments(scheduled_at);
create index idx_appointments_status       on public.appointments(status);

alter table public.appointments enable row level security;

create policy "org_isolation" on public.appointments
  using      (org_id = (auth.jwt() ->> 'org_id')::uuid)
  with check (org_id = (auth.jwt() ->> 'org_id')::uuid);

create trigger trg_appointments_updated_at
  before update on public.appointments
  for each row execute function public.set_updated_at();
