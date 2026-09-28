# DATABASE_SCHEMA.md — Supabase (Postgres)

## Design Rules
1. Every table has `org_id uuid NOT NULL` — enforced at DB level
2. RLS policy on every table: `org_id = (auth.jwt() ->> 'org_id')::uuid`
3. `id` is always `uuid` with `DEFAULT gen_random_uuid()`
4. Timestamps always `timestamptz DEFAULT now()`

---

## Tables

### `organizations`
> Tenant root. One row = one dealership.

```sql
CREATE TABLE organizations (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug                varchar(50) UNIQUE NOT NULL,       -- e.g. "example-dealership"
  name                text NOT NULL,
  logo_url            text,
  whatsapp_phone_id   text UNIQUE NOT NULL,              -- from Meta dashboard
  pinecone_namespace  text UNIQUE NOT NULL,              -- "org_{uuid}"
  plan                text DEFAULT 'free',               -- free | pro | enterprise
  settings            jsonb DEFAULT '{}',                -- system_prompt, lang, model
  is_active           bool DEFAULT true,
  created_at          timestamptz DEFAULT now()
);
-- No RLS needed — only service_role key touches this table
```

**`settings` jsonb shape:**
```json
{
  "system_prompt": "You are a helpful sales assistant for...",
  "language": "en",
  "llm_model": "gpt-4o-mini",
  "handoff_message": "Let me connect you with our team.",
  "max_history_turns": 10
}
```

---

### `users`
> Dealer admins and sales agents. Linked to Supabase Auth.

```sql
CREATE TABLE users (
  id          uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  email       text UNIQUE NOT NULL,
  full_name   text,
  phone       varchar(20),
  role        text NOT NULL DEFAULT 'agent',   -- superadmin | admin | agent | viewer
  avatar_url  text,
  last_seen   timestamptz,
  is_active   bool DEFAULT true,
  created_at  timestamptz DEFAULT now()
);

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
CREATE POLICY "org_isolation" ON users
  USING (org_id = (auth.jwt() ->> 'org_id')::uuid);
```

---

### `leads`
> Every customer contact captured from WhatsApp.

```sql
CREATE TABLE leads (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  assigned_to  uuid REFERENCES users(id),
  name         text,
  phone        varchar(20) NOT NULL,           -- WhatsApp phone number
  email        text,
  channel      text DEFAULT 'whatsapp',        -- whatsapp | web | api
  status       text DEFAULT 'new',             -- new | hot | warm | cold | converted | lost
  tags         text[] DEFAULT '{}',
  meta         jsonb DEFAULT '{}',             -- UTM, source page, etc.
  created_at   timestamptz DEFAULT now(),
  updated_at   timestamptz DEFAULT now()
);

ALTER TABLE leads ENABLE ROW LEVEL SECURITY;
CREATE POLICY "org_isolation" ON leads
  USING (org_id = (auth.jwt() ->> 'org_id')::uuid);

CREATE INDEX idx_leads_org ON leads(org_id);
CREATE INDEX idx_leads_phone ON leads(org_id, phone);
CREATE INDEX idx_leads_status ON leads(org_id, status);
```

---

### `conversations`
> One WhatsApp thread = one conversation.

```sql
CREATE TABLE conversations (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  lead_id       uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  wa_thread_id  text,                          -- Meta conversation ID if available
  mode          text DEFAULT 'ai',             -- 'ai' | 'human'  ← THE KEY FIELD
  assigned_to   uuid REFERENCES users(id),     -- set when mode = 'human'
  status        text DEFAULT 'active',         -- active | resolved | escalated
  resolved_at   timestamptz,
  created_at    timestamptz DEFAULT now(),
  updated_at    timestamptz DEFAULT now()
);

ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
CREATE POLICY "org_isolation" ON conversations
  USING (org_id = (auth.jwt() ->> 'org_id')::uuid);

CREATE INDEX idx_conv_lead ON conversations(lead_id);
CREATE INDEX idx_conv_org_active ON conversations(org_id, status);
```

---

### `messages`
> Every single message in every conversation.

```sql
CREATE TABLE messages (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  org_id           uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  role             text NOT NULL,              -- 'user' | 'assistant' | 'agent'
  content          text NOT NULL,
  sources_used     jsonb DEFAULT '[]',         -- [{chunk_id, doc_title, score}]
  tokens_used      int DEFAULT 0,
  latency_ms       int,
  created_at       timestamptz DEFAULT now()
);

ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
CREATE POLICY "org_isolation" ON messages
  USING (org_id = (auth.jwt() ->> 'org_id')::uuid);

CREATE INDEX idx_msg_conv ON messages(conversation_id, created_at);
```

---

### `kb_documents`
> Tracks every knowledge source uploaded per org.

```sql
CREATE TABLE kb_documents (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  uploaded_by  uuid REFERENCES users(id),
  source_type  text NOT NULL,                  -- 'url' | 'pdf' | 'docx' | 'text'
  source_url   text,                           -- for url type
  file_path    text,                           -- Supabase Storage path for files
  title        text,
  status       text DEFAULT 'pending',         -- pending | processing | indexed | failed
  chunk_count  int DEFAULT 0,
  error_msg    text,
  rescrape_at  timestamptz,                    -- for scheduled URL re-crawls
  created_at   timestamptz DEFAULT now(),
  updated_at   timestamptz DEFAULT now()
);

ALTER TABLE kb_documents ENABLE ROW LEVEL SECURITY;
CREATE POLICY "org_isolation" ON kb_documents
  USING (org_id = (auth.jwt() ->> 'org_id')::uuid);
```

---

### `kb_chunks`
> Maps Pinecone vector IDs back to source documents.  
> This is the bridge between Supabase and Pinecone.

```sql
CREATE TABLE kb_chunks (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id             uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  doc_id             uuid NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
  pinecone_vector_id text UNIQUE NOT NULL,    -- ← the bridge to Pinecone
  chunk_index        int NOT NULL,
  content_preview    text,                    -- first 200 chars for display
  token_count        int,
  created_at         timestamptz DEFAULT now()
);

ALTER TABLE kb_chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY "org_isolation" ON kb_chunks
  USING (org_id = (auth.jwt() ->> 'org_id')::uuid);

CREATE INDEX idx_chunk_doc ON kb_chunks(doc_id);
CREATE INDEX idx_chunk_vector ON kb_chunks(pinecone_vector_id);
```

---

### `notifications`
> In-app and WhatsApp alerts for agents.

```sql
CREATE TABLE notifications (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id     uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id    uuid REFERENCES users(id) ON DELETE CASCADE,
  lead_id    uuid REFERENCES leads(id),
  type       text NOT NULL,           -- 'new_lead' | 'escalation' | 'assigned' | 'resolved'
  channel    text DEFAULT 'in_app',   -- 'in_app' | 'whatsapp'
  title      text NOT NULL,
  body       text,
  is_read    bool DEFAULT false,
  sent_at    timestamptz DEFAULT now()
);

ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
CREATE POLICY "org_isolation" ON notifications
  USING (org_id = (auth.jwt() ->> 'org_id')::uuid);
```

---

## Supabase Storage Buckets

```
Bucket: dealership-files  (private)
  Policy: users can only read/write path /orgs/{their_org_id}/*

Path structure:
  /orgs/{org_id}/docs/{uuid}_{filename}.pdf
  /orgs/{org_id}/docs/{uuid}_{filename}.docx
  /orgs/{org_id}/logo.png
```

---

## Supabase Auth — JWT Custom Claim

When a user logs in, inject `org_id` into their JWT so RLS works automatically:

```sql
-- Supabase Auth Hook (Database Function)
CREATE OR REPLACE FUNCTION public.custom_access_token_hook(event jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  claims jsonb;
  user_org_id uuid;
BEGIN
  SELECT org_id INTO user_org_id FROM public.users WHERE id = (event->>'userId')::uuid;
  claims := event->'claims';
  claims := jsonb_set(claims, '{org_id}', to_jsonb(user_org_id));
  RETURN jsonb_set(event, '{claims}', claims);
END;
$$;
```

---

## Entity Relationships

```
organizations
    ├── users (org_id FK)
    ├── leads (org_id FK)
    │     └── conversations (lead_id FK)
    │               └── messages (conversation_id FK)
    ├── kb_documents (org_id FK)
    │     └── kb_chunks (doc_id FK) ←──→ Pinecone vectors
    └── notifications (org_id FK)
```