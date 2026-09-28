create table public.kb_documents (
  id          uuid        primary key default gen_random_uuid(),
  org_id      uuid        not null references public.organizations(id) on delete cascade,
  uploaded_by uuid        references public.users(id) on delete set null,
  source_type text        not null,
  source_url  text,
  file_path   text,
  status      text        not null default 'pending'
                check (status in ('pending', 'processing', 'indexed', 'failed')),
  chunk_count integer     not null default 0,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index idx_kb_documents_org_id      on public.kb_documents(org_id);
create index idx_kb_documents_uploaded_by on public.kb_documents(uploaded_by);

create trigger trg_kb_documents_updated_at
  before update on public.kb_documents
  for each row execute function public.set_updated_at();
