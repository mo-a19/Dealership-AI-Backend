-- Append-only: no updated_at, no update trigger
create table public.kb_chunks (
  id                 uuid        primary key default gen_random_uuid(),
  org_id             uuid        not null references public.organizations(id) on delete cascade,
  doc_id             uuid        not null references public.kb_documents(id) on delete cascade,
  pinecone_vector_id text        not null unique,
  chunk_index        integer     not null,
  content_preview    text,
  created_at         timestamptz not null default now()
);

create index idx_kb_chunks_org_id on public.kb_chunks(org_id);
create index idx_kb_chunks_doc_id on public.kb_chunks(doc_id);
