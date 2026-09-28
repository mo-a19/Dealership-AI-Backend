-- Grant schema usage to all roles
grant usage on schema public to anon, authenticated, service_role;

-- service_role: full access, bypasses RLS (used by backend with service key)
grant all on all tables    in schema public to service_role;
grant all on all sequences in schema public to service_role;
grant all on all routines  in schema public to service_role;

-- authenticated: scoped by RLS policies in 009_rls_policies.sql
grant select, insert, update, delete on
  public.organizations,
  public.users,
  public.leads,
  public.conversations,
  public.messages,
  public.kb_documents,
  public.kb_chunks,
  public.notifications
to authenticated;

-- anon: read-only on nothing sensitive (extend as needed)
-- No table grants for anon by default — add explicitly if a public read is required.

-- Ensure future tables created in this schema inherit grants for service_role
alter default privileges in schema public
  grant all on tables    to service_role;
alter default privileges in schema public
  grant all on sequences to service_role;
alter default privileges in schema public
  grant all on routines  to service_role;
