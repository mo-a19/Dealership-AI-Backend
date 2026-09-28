# API_ENDPOINTS.md — FastAPI Route Reference

## Status Key
- ✅ Done (exists in codebase)
- 🔴 Missing (needs to be built)
- ⚠️ Partial (exists but needs changes)

---

## WhatsApp Webhook

| Method | Route | Status | Description |
|--------|-------|--------|-------------|
| GET | `/webhook` | 🔴 | Meta verification handshake (returns `hub.challenge`) |
| POST | `/webhook` | 🔴 | Receives all WA messages → resolves org → dispatches |

### POST `/webhook` — Core Logic
```python
# 1. Verify Meta signature (X-Hub-Signature-256 header)
# 2. Extract phone_number_id + customer phone + message text
# 3. Supabase: SELECT * FROM organizations WHERE whatsapp_phone_id = ?
# 4. Supabase: upsert lead by (org_id, phone)
# 5. Supabase: get or create conversation
# 6. Check conversations.mode:
#    - "human" → save message, push Realtime, stop
#    - "ai"    → call RAG service → send reply via Meta API
```

---

## Knowledge Base

| Method | Route | Status | Description |
|--------|-------|--------|-------------|
| POST | `/kb/ingest` | ⚠️ | Submit URL or file for ingestion (needs org_id + Supabase write) |
| GET | `/kb/status/{doc_id}` | ⚠️ | Ingestion job status (currently uses in-memory state) |
| GET | `/kb/documents` | 🔴 | List all docs for org (paginated) |
| DELETE | `/kb/documents/{doc_id}` | 🔴 | Delete doc + its Pinecone vectors + kb_chunks rows |

### POST `/kb/ingest` — Request Body
```json
{
  "source_type": "url",
  "source_url": "https://example-dealership.com/inventory",
  "title": "Car Inventory Page"
}
```
```json
{
  "source_type": "pdf",
  "title": "Price List Q1 2025"
  // file uploaded as multipart/form-data
}
```

---

## Conversations & Messages

| Method | Route | Status | Description |
|--------|-------|--------|-------------|
| GET | `/conversations` | 🔴 | List org's conversations (filter by status, mode) |
| GET | `/conversations/{id}` | 🔴 | Single conversation with messages |
| PATCH | `/conversations/{id}/mode` | 🔴 | **Human takeover toggle** |
| POST | `/conversations/{id}/messages` | 🔴 | Agent sends message (human mode only) |

### PATCH `/conversations/{id}/mode` — Takeover
```json
// Take over
{ "mode": "human", "assigned_to": "agent-uuid" }

// Hand back to AI
{ "mode": "ai" }
```

---

## Leads

| Method | Route | Status | Description |
|--------|-------|--------|-------------|
| GET | `/leads` | 🔴 | List leads (filter by status, assigned_to) |
| GET | `/leads/{id}` | 🔴 | Lead detail + conversation history |
| PATCH | `/leads/{id}` | 🔴 | Update status, assign agent, add tags |

---

## Chat & Query (existing, need org_id wired in)

| Method | Route | Status | Description |
|--------|-------|--------|-------------|
| POST | `/query` | ⚠️ | Stateless RAG query (needs org_id + Pinecone namespace) |
| POST | `/chat` | ⚠️ | Multi-turn chat (replace in-memory with Supabase) |
| GET | `/chat/{session_id}` | ⚠️ | Get session history |
| DELETE | `/chat/{session_id}` | ⚠️ | Clear session |

---

## Auth (Supabase handles, FastAPI validates)

| Method | Route | Status | Description |
|--------|-------|--------|-------------|
| — | JWT middleware | 🔴 | Extract `org_id` from Supabase JWT on every request |
| POST | `/auth/invite` | 🔴 | Superadmin invites dealer admin (Supabase invite email) |

### JWT Middleware Pattern
```python
async def get_current_org(token: str = Depends(oauth2_scheme)):
    payload = supabase.auth.get_user(token)
    org_id = payload.user.user_metadata.get("org_id")
    return org_id
```

---

## Health

| Method | Route | Status | Description |
|--------|-------|--------|-------------|
| GET | `/health` | ✅ | Checks Pinecone + OpenAI connectivity |

---

## Request Auth Headers

```
Authorization: Bearer <supabase-jwt>     # for dashboard users
X-API-Key: <hashed-api-key>              # for external integrations
X-Hub-Signature-256: sha256=<sig>        # from Meta on /webhook only
```