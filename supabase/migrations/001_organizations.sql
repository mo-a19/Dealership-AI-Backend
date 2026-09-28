-- Enable Supabase Vault for encrypted secret storage (wasender keys live here)
create extension if not exists "supabase_vault" schema extensions;

-- Shared trigger function used by all tables with updated_at
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create table public.organizations (
  id                         uuid        primary key default gen_random_uuid(),
  slug                       text        not null unique,
  name                       text        not null,
  whatsapp_phone_id          text        unique,
  -- WasenderAPI: one session per dealership; raw keys stored in vault.secrets
  wasender_session_id        integer     unique,
  wasender_api_key_id        uuid,
  wasender_webhook_secret_id uuid,
  wasender_status            text        not null default 'disconnected',
  pinecone_namespace         text        unique,
  plan                       text        not null default 'starter',
  settings                   jsonb       not null default '{}',
  is_active                  boolean     not null default true,
  created_at                 timestamptz not null default now(),
  updated_at                 timestamptz not null default now()
);

create trigger trg_organizations_updated_at
  before update on public.organizations
  for each row execute function public.set_updated_at();
