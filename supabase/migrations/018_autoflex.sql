-- Autoflex (CRM/DMS) per-dealership credentials + sync state.
-- Mirrors the wasender_* pattern in 001_organizations.sql: raw secrets live in
-- vault.secrets, only their uuid ids are stored here. api_url is the authoritative
-- endpoint returned by /authenticate (resolved per environment, never hardcoded).
alter table public.organizations
  add column autoflex_api_key_id   uuid,
  add column autoflex_username_id  uuid,
  add column autoflex_password_id  uuid,
  add column autoflex_api_url      text,
  add column autoflex_enabled      boolean     not null default false,
  add column autoflex_last_sync_at timestamptz;
