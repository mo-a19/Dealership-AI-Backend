-- content_hash: skip re-embedding vehicles whose text hasn't changed
alter table public.kb_chunks add column content_hash text;

-- One kb_documents row per org for the vehicle catalog (stable across syncs)
create unique index idx_kb_documents_autoflex_org
  on public.kb_documents (org_id)
  where source_type = 'autoflex';
