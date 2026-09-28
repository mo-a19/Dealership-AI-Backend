-- Custom Access Token Hook: injects org_id + role as top-level JWT claims.
-- RLS reads: auth.jwt() ->> 'org_id'
-- Middleware reads: claims['org_id'], claims['role']
--
-- MANUAL STEP after running:
--   Dashboard → Authentication → Hooks → Customize Access Token (JWT) Claims
--   → select public.custom_access_token_hook
create or replace function public.custom_access_token_hook(event jsonb)
returns jsonb language plpgsql stable security definer as $$
declare
  v_org_id uuid;
  v_role   text;
begin
  select org_id, role into v_org_id, v_role
    from public.users
   where id = (event ->> 'user_id')::uuid;

  if v_org_id is not null then
    event := jsonb_set(event, '{claims,org_id}', to_jsonb(v_org_id::text));
  end if;

  if v_role is not null then
    event := jsonb_set(event, '{claims,role}', to_jsonb(v_role));
  end if;

  return event;
end;
$$;

grant execute on function public.custom_access_token_hook to supabase_auth_admin;
grant select on public.users to supabase_auth_admin;

drop policy if exists "auth_admin_read" on public.users;
create policy "auth_admin_read" on public.users
  as permissive for select
  to supabase_auth_admin
  using (true);

revoke execute on function public.custom_access_token_hook from anon, authenticated, public;
