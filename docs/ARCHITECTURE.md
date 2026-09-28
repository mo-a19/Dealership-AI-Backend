# ARCHITECTURE.md — Dealership AI Full System Design

## System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         CLIENTS                                      │
│  📱 WhatsApp User          🖥  Dealer Dashboard (Next.js)           │
└────────────┬───────────────────────────┬────────────────────────────┘
             │ Meta Cloud API webhook     │ REST + Supabase Realtime
             ▼                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      FASTAPI BACKEND                                 │
│                                                                      │
│  /webhook ──► Org Resolver ──► Mode Check (ai/human)                │
│                                    │              │                  │
│                               RAG Service    Takeover Svc           │
│                                    │              │                  │
│                          Lead Classifier    Notify Service          │
│                                                                      │
│  /kb/ingest ──► [Celery Worker] ──► Chunk ──► Embed ──► Upsert     │
└──────┬────────────────────┬─────────────────────┬───────────────────┘
       │                    │                     │
       ▼                    ▼                     ▼
┌─────────────┐   ┌──────────────────┐   ┌──────────────┐
│  Supabase   │   │    Pinecone       │   │  Meta Cloud  │
│  Postgres   │   │  Vector Index     │   │     API      │
│             │   │                  │   │              │
│ orgs        │   │ namespace per    │   │ send reply   │
│ users       │   │ org_{uuid}       │   │ to WA user   │
│ leads       │   │                  │   │              │
│ convs       │◄──► kb_chunks        │   └──────────────┘
│ messages    │   │ (vector_id FK)   │
│ kb_docs     │   └──────────────────┘
│ kb_chunks   │
│ notifs      │
└─────────────┘
```

---

## Layer Responsibilities

### 1. Meta Cloud API (WhatsApp)
- Receives customer messages, sends them to your `/webhook`
- You call it back to send replies to the customer
- One `phone_number_id` maps to one dealership org

### 2. FastAPI Backend
Every request starts here. Key responsibilities:

| Service | File | Does |
|---------|------|------|
| Webhook Router | `routes/webhook.py` | Resolves org, checks mode, dispatches |
| RAG Service | `services/retrieval.py` | Embed → Pinecone → LLM → answer |
| Ingest Service | `services/ingestion.py` | URL/PDF → chunks → Pinecone |
| Lead Classifier | `services/lead_classifier.py` | Detects hot/warm/cold intent |
| Takeover Service | `services/takeover.py` | Flips `conversations.mode` |
| Notify Service | `services/notify.py` | Pushes alerts to dashboard + WA |

### 3. Supabase (Postgres)
**All structured business data.** Row Level Security enforces per-org isolation at the database layer — no application code can accidentally leak data between orgs.

### 4. Pinecone (Vector DB)
**AI knowledge only.** One index, one namespace per org (`org_{uuid}`). Stores embeddings of chunked documents. Never stores raw text — that stays in `kb_chunks` table in Supabase.

### 5. Dealer Dashboard (Next.js)
- Connects to FastAPI for actions (ingest, takeover, send message)
- Connects to Supabase Realtime for live updates (new leads, incoming messages)

---

## Data Flow Diagrams

### A. WhatsApp Chat Flow

```mermaid
sequenceDiagram
    participant C as Customer (WA)
    participant M as Meta Cloud API
    participant F as FastAPI /webhook
    participant S as Supabase
    participant P as Pinecone
    participant L as LLM (OpenAI)

    C->>M: sends WhatsApp message
    M->>F: POST /webhook (phone_number_id, message)
    F->>S: lookup org by phone_number_id
    S-->>F: org_id, pinecone_namespace, system_prompt
    F->>S: find/create lead by phone number
    F->>S: load conversation history (last 10 msgs)
    F->>S: check conversations.mode

    alt mode = "human"
        F->>S: save message, trigger Realtime
        Note over F: skip AI, agent replies manually
    else mode = "ai"
        F->>P: query namespace=org_{id}, top-5 chunks
        P-->>F: relevant context chunks
        F->>L: prompt = system_prompt + context + history + message
        L-->>F: AI answer
        F->>S: INSERT messages (user + assistant)
        F->>M: send reply to customer
        M-->>C: AI answer delivered
    end
```

### B. Knowledge Base Ingestion Flow

```mermaid
sequenceDiagram
    participant A as Admin (Dashboard)
    participant F as FastAPI /kb/ingest
    participant S as Supabase
    participant W as Celery Worker
    participant O as OpenAI Embed API
    participant P as Pinecone

    A->>F: POST /kb/ingest (url or file upload)
    F->>S: INSERT kb_documents (status=pending)
    F->>S: upload file to Storage /orgs/{org_id}/
    F-->>A: { doc_id, status: "pending" }
    F->>W: queue ingestion task (doc_id, org_id)

    W->>S: fetch doc metadata
    W->>W: crawl URL or parse PDF
    W->>W: semantic chunk (512 tokens, 50 overlap)
    W->>O: batch embed all chunks
    O-->>W: float vectors
    W->>P: upsert vectors to namespace=org_{id}
    W->>S: INSERT kb_chunks (one row per chunk, pinecone_vector_id)
    W->>S: UPDATE kb_documents SET status='indexed', chunk_count=N
    S->>A: Realtime event → dashboard updates status
```

### C. Human Takeover Flow

```mermaid
sequenceDiagram
    participant AG as Agent (Dashboard)
    participant F as FastAPI
    participant S as Supabase
    participant M as Meta Cloud API
    participant C as Customer (WA)

    Note over AG: sees live conversation in dashboard
    AG->>F: PATCH /conversations/{id}/mode { mode: "human" }
    F->>S: UPDATE conversations SET mode='human', assigned_to=agent_id
    S->>AG: Realtime push → conversation locked to agent

    C->>M: customer sends next message
    M->>F: POST /webhook
    F->>S: check mode → "human"
    F->>S: INSERT message (role=user)
    S->>AG: Realtime → new message appears in dashboard

    AG->>F: POST /messages { conversation_id, content }
    F->>S: INSERT message (role=agent)
    F->>M: send via Meta API
    M->>C: agent reply delivered

    AG->>F: PATCH /conversations/{id}/mode { mode: "ai" }
    F->>S: UPDATE conversations SET mode='ai'
    Note over F: AI takes over again automatically
```

---

## Environment Variables

```env
# FastAPI
SECRET_KEY=

# Supabase
SUPABASE_URL=
SUPABASE_SERVICE_KEY=         # server-side only, never expose to client
SUPABASE_ANON_KEY=

# Pinecone
PINECONE_API_KEY=
PINECONE_INDEX_NAME=dealership-ai

# OpenAI
OPENAI_API_KEY=

# Meta WhatsApp
META_VERIFY_TOKEN=            # for webhook verification
META_APP_SECRET=
META_API_VERSION=v19.0

# Redis (Upstash)
REDIS_URL=
```