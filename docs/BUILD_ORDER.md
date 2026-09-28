# BUILD_ORDER.md — What to Build Next

Based on codebase audit. Current state: RAG engine ✅, crawlers ✅, chunker ✅, Pinecone ✅.  
Everything below is missing or needs wiring.

---

## Priority 1 — Foundation (build these first, everything depends on them)

### 1.1 Supabase migrations
Create all tables with RLS before writing any route.

```bash
supabase/migrations/
  001_organizations.sql
  002_users.sql
  003_leads.sql
  004_conversations.sql
  005_messages.sql
  006_kb_documents.sql
  007_kb_chunks.sql
  008_notifications.sql
  009_rls_policies.sql
  010_auth_hook.sql       ← injects org_id into JWT
```

### 1.2 Fix `retrieval.py` — remove hardcoded persona
```python
# Line 18–34 in retrieval.py
# Change from:
_SYSTEM_PROMPT = "You are a car sales consultant..."

# To: accept as parameter
async def get_answer(query: str, org_id: str, system_prompt: str):
    ...
```
Fetch `system_prompt` from `organizations.settings` at the start of each request.

### 1.3 FastAPI JWT middleware
```python
# app/middleware/auth.py
async def get_current_org(token: str = Depends(oauth2_scheme)) -> str:
    user = supabase.auth.get_user(token)
    return user.user_metadata["org_id"]
```

---

## Priority 2 — WhatsApp Webhook (core product)

### 2.1 `app/routes/webhook.py`
```python
GET  /webhook   → Meta verification (returns hub.challenge)
POST /webhook   → main handler
```

Logic inside POST:
1. Verify `X-Hub-Signature-256` header
2. Extract `phone_number_id`, customer `phone`, `message`
3. Lookup org by `phone_number_id`
4. Upsert lead by `(org_id, phone)`
5. Get or create conversation
6. Check `conversations.mode`:
   - `"human"` → save message to DB, Supabase Realtime fires, stop
   - `"ai"` → call RAG → send reply via Meta API

### 2.2 Replace `app/state.py` sessions with Supabase
- Remove `state.sessions` dict
- Load last N messages from `messages` table instead
- Remove `state.jobs` dict
- Update `kb_documents.status` in DB instead

---

## Priority 3 — Human Takeover

### 3.1 `PATCH /conversations/{id}/mode`
```python
{ "mode": "human", "assigned_to": "agent-uuid" }  # take over
{ "mode": "ai" }                                    # hand back
```

### 3.2 `POST /conversations/{id}/messages`
Agent sends a message in human mode → FastAPI → Meta Send API → customer.

### 3.3 Supabase Realtime on `conversations` + `messages`
Dashboard subscribes to `conversations` table changes → live updates without polling.

---

## Priority 4 — Ingestion wired to Supabase

Current `ingestion.py` works but doesn't update any DB.  
Wire it to:
- Read `kb_documents` row to get file path / URL
- Write `kb_chunks` rows after upsert to Pinecone
- Update `kb_documents.status` → `indexed` / `failed`

---

## Priority 5 — Lead Classifier

New file: `app/services/lead_classifier.py`

Simple keyword + LLM-based classifier:
```python
# Input: last 3 messages from conversation
# Output: "hot" | "warm" | "cold"
# Hot signals: "price", "available", "test drive", "buy", "today"
# On "hot": UPDATE leads SET status='hot', trigger notification
```

---

## Priority 6 — Notifications

New file: `app/services/notify.py`

```python
async def notify_new_lead(org_id, lead_id):
    # 1. INSERT notifications row
    # 2. Supabase Realtime fires automatically to dashboard
    # 3. Optionally: send WhatsApp message to assigned agent
```

---

## What NOT to build yet
- Email notifications (WhatsApp is enough for now)
- Billing / usage metering
- Multi-language support
- Re-scraping scheduler (Celery Beat)

---

## Recommended Build Sequence

```
Week 1:  Supabase migrations + RLS + Auth hook + JWT middleware
Week 1:  Fix retrieval.py system prompt (1 hour task)
Week 2:  /webhook route (Meta verification + message handler)
Week 2:  Replace app/state.py with Supabase reads/writes
Week 3:  Human takeover endpoints + Realtime on dashboard
Week 3:  Wire ingestion.py → kb_chunks + kb_documents status
Week 4:  Lead classifier + notifications
```