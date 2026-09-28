# MULTI_TENANT.md — Isolation Strategy

Every dealership's data is **completely isolated** across all layers.  
No application-level filtering — isolation is enforced at the infrastructure level.

---

## Layer 1 — Supabase Row Level Security

Every table has this policy:

```sql
CREATE POLICY "org_isolation" ON <table>
  USING (org_id = (auth.jwt() ->> 'org_id')::uuid);
```

**How it works:**
1. User logs in → Supabase Auth issues JWT
2. JWT contains `org_id` claim (injected via Auth Hook)
3. Every DB query automatically filters by that `org_id`
4. A query like `SELECT * FROM leads` only ever returns that org's leads
5. No code change needed — it's enforced at the Postgres level

**Roles:**
```
superadmin  → bypasses RLS (uses service_role key, server-side only)
admin       → full access to their org
agent       → read leads + conversations, write messages
viewer      → read-only
```

---

## Layer 2 — Pinecone Namespaces

```
One Pinecone index: "dealership-ai"
One namespace per org: "org_{uuid}"

Dealership A → namespace: org_a1b2c3d4...
Dealership B → namespace: org_e5f6g7h8...
```

**On ingest:**
```python
pinecone_index.upsert(
    vectors=[(vector_id, embedding, metadata)],
    namespace=f"org_{org_id}"   # ← hard scoped
)
```

**On query:**
```python
results = pinecone_index.query(
    vector=query_embedding,
    namespace=f"org_{org_id}",  # ← only searches this org's vectors
    top_k=5
)
```

**On org deletion:**
```python
pinecone_index.delete(delete_all=True, namespace=f"org_{org_id}")
```

---

## Layer 3 — FastAPI Middleware

```python
# Every authenticated route:
async def get_org(token = Depends(oauth2_scheme)) -> str:
    user = supabase.auth.get_user(token)
    return user.user_metadata["org_id"]

# Every service call passes org_id explicitly:
@router.get("/leads")
async def get_leads(org_id: str = Depends(get_org)):
    return supabase.table("leads").select("*").eq("org_id", org_id).execute()
```

**WhatsApp webhook (no JWT — resolves org differently):**
```python
# Webhook identifies org by phone_number_id from Meta payload
org = supabase.table("organizations") \
    .select("*") \
    .eq("whatsapp_phone_id", phone_number_id) \
    .single().execute()
org_id = org.data["id"]
```

---

## Layer 4 — Supabase Storage

```
Bucket: dealership-files (private)

Storage RLS policy:
  Users can only access paths starting with /orgs/{their_org_id}/

File paths:
  /orgs/a1b2c3.../docs/uuid_pricelist.pdf
  /orgs/e5f6g7.../docs/uuid_inventory.pdf
```

---

## Layer 5 — AI Prompt Isolation

Each org has its own system prompt stored in `organizations.settings`:

```json
{
  "system_prompt": "You are a helpful assistant for Example Dealership..."
}
```

The LLM only receives:
- That org's system prompt
- That org's retrieved Pinecone chunks (from their namespace)
- That conversation's message history

**There is no shared AI context between dealerships.**