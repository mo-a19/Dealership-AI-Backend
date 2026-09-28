# Dealership AI — System Architecture

> WhatsApp-first, multi-tenant AI chat platform for car dealerships.  
> Built on **FastAPI · Supabase · Pinecone · Meta Cloud API**

---

## What This System Does

Each dealership (org) gets:
- A **WhatsApp number** that their customers chat with
- An **AI that answers** from that dealership's own knowledge base
- A **dashboard** where admins upload docs and agents manage leads
- **Human takeover** — any agent can intercept a live AI conversation

---

## Repository Structure

```
dealership-ai/
├── app/
│   ├── main.py                  # FastAPI entry, router registration
│   ├── config.py                # Pydantic settings from .env
│   ├── state.py                 # ⚠ in-memory (replace with Supabase)
│   ├── models/
│   │   └── schemas.py           # All Pydantic request/response models
│   ├── routes/
│   │   ├── webhook.py           # 🔴 MISSING — WhatsApp webhook handler
│   │   ├── scraper.py           # POST /scrape, GET /status/{id}
│   │   ├── query.py             # POST /query — stateless RAG
│   │   └── chat.py              # POST /chat — multi-turn (in-memory)
│   ├── services/
│   │   ├── ingestion.py         # crawl → chunk → embed → upsert pipeline
│   │   ├── chunker.py           # semantic HTML chunker
│   │   ├── retrieval.py         # agentic RAG loop (⚠ hardcoded dealership prompt)
│   │   ├── lead_classifier.py   # 🔴 MISSING
│   │   └── takeover.py          # 🔴 MISSING
│   └── utils/
│       ├── scraper.py           # async httpx BFS crawler
│       ├── browser_scraper.py   # Playwright crawler for JS-heavy sites
│       ├── embeddings.py        # OpenAI embed with retry + batching
│       └── pinecone_client.py   # singleton client, ensure_index()
├── docs/                        # ← this folder
│   ├── README.md                # this file
│   ├── ARCHITECTURE.md          # full system design
│   ├── DATABASE_SCHEMA.md       # all Supabase tables
│   ├── API_ENDPOINTS.md         # all FastAPI routes
│   ├── MULTI_TENANT.md          # isolation strategy
│   └── BUILD_ORDER.md           # what to build next + priority
└── .env.example
```

---

## Docs Index

| Doc | What it covers |
|-----|---------------|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Full system layers, data flow, component responsibilities |
| [DATABASE_SCHEMA.md](./DATABASE_SCHEMA.md) | Every Supabase table, columns, RLS rules |
| [API_ENDPOINTS.md](./API_ENDPOINTS.md) | All FastAPI routes — done, missing, and spec for what to build |
| [MULTI_TENANT.md](./MULTI_TENANT.md) | How Supabase RLS + Pinecone namespaces isolate each dealership |
| [BUILD_ORDER.md](./BUILD_ORDER.md) | Prioritized build list based on current codebase state |

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| API | FastAPI (Python) | All backend logic, webhook handler |
| Structured DB | Supabase (Postgres) | Orgs, users, leads, conversations, messages |
| Vector DB | Pinecone | Embeddings for each org's knowledge base |
| File Storage | Supabase Storage | PDFs, DOCX uploads per org |
| Auth | Supabase Auth | JWT with `org_id` claim injected |
| Realtime | Supabase Realtime | Live dashboard updates, takeover events |
| Messaging | Meta Cloud API | WhatsApp send/receive |
| Embeddings | OpenAI `text-embedding-3-small` | Chunk + query embedding |
| LLM | OpenAI / Anthropic | Answer generation |
| Queue | Celery + Upstash Redis | Async ingestion jobs |

---

## The One Key Field

```
conversations.mode = "ai"     →  RAG engine handles automatically
conversations.mode = "human"  →  AI skipped, agent replies from dashboard
```

This single column controls the entire human takeover mechanism.