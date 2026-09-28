-- rag_sessions was created after the initial grants migration (011)
grant select, insert, update, delete on public.rag_sessions to authenticated;
grant all on public.rag_sessions to service_role;
