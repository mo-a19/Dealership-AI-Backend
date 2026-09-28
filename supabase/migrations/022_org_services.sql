-- Services offered by each dealership.
-- Each entry: { type, label, auto_schedule, booking_url, enabled }
-- auto_schedule=true  → bot sends booking_url directly
-- auto_schedule=false → bot collects details, staff confirms availability

alter table public.organizations
  add column if not exists services jsonb not null default '[]';
