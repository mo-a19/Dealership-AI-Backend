-- Vault helpers so the backend (service_role, via PostgREST RPC) can store and
-- read WaSender secrets without exposing the `vault` schema directly.
-- supabase_vault is already enabled in 001_organizations.sql.
--
-- vault_write is idempotent on `name`: re-running connect for an org updates the
-- existing secret instead of creating a duplicate.
create or replace function public.vault_write(p_name text, p_secret text)
returns uuid
language plpgsql
security definer
set search_path = public, vault, extensions
as $$
declare
  v_id uuid;
begin
  select id into v_id from vault.secrets where name = p_name;
  if v_id is null then
    v_id := vault.create_secret(p_secret, p_name);
  else
    perform vault.update_secret(v_id, p_secret);
  end if;
  return v_id;
end;
$$;

create or replace function public.vault_read(p_secret_id uuid)
returns text
language plpgsql
security definer
set search_path = public, vault, extensions
as $$
declare
  v_secret text;
begin
  select decrypted_secret into v_secret
    from vault.decrypted_secrets
   where id = p_secret_id;
  return v_secret;
end;
$$;

-- Backend-only: never reachable by tenant JWTs.
revoke all on function public.vault_write(text, text) from anon, authenticated, public;
revoke all on function public.vault_read(uuid)        from anon, authenticated, public;
grant execute on function public.vault_write(text, text) to service_role;
grant execute on function public.vault_read(uuid)        to service_role;
