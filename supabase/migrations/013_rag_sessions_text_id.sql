alter table public.rag_sessions
  alter column id type text using id::text;
