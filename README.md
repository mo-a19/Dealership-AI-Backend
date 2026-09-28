<div align="center">

# Dealership AI — WhatsApp AI Assistant for Car Dealerships

**Built by Mo A** — Backend / Full-Stack Engineer (Python · FastAPI)

![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat&logo=fastapi&logoColor=white)
![Supabase](https://img.shields.io/badge/Supabase-3ECF8E?style=flat&logo=supabase&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?style=flat&logo=postgresql&logoColor=white)
![Pinecone](https://img.shields.io/badge/Pinecone-000000?style=flat&logo=pinecone&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-412991?style=flat&logo=openai&logoColor=white)
![WhatsApp](https://img.shields.io/badge/WhatsApp-25D366?style=flat&logo=whatsapp&logoColor=white)

A multi-tenant SaaS backend that lets car dealerships automate customer conversations on WhatsApp with a retrieval-augmented AI assistant grounded in each dealership's own inventory and documents.

</div>

---

## Overview

Each dealership (tenant) gets an isolated workspace with its own users, leads, conversations, knowledge base and WhatsApp connection. The AI answers customers from that dealership's data only, and a human agent can take over any live conversation at any time.

## Key Features

- **Multi-tenant architecture** — Postgres Row Level Security plus per-tenant vector namespaces for strict data isolation
- **Agentic RAG** — tool-calling retrieval loop over website crawls, uploaded PDFs and live vehicle inventory
- **WhatsApp automation** — webhook-driven messaging, session management and human takeover
- **Knowledge ingestion** — async web crawler (with headless-browser fallback for JS-heavy sites), semantic chunking, PDF ingestion with optional vision
- **Vehicle inventory sync** — DMS/CRM integration for inventory, plus Dutch RDW licence-plate lookup
- **Appointments and business hours** — configurable services, availability and booking
- **Auth and roles** — Supabase Auth with JWT claims and role-based access
- **CI/CD** — GitHub Actions deployment workflow

## Tech Stack

| Layer | Technology |
|---|---|
| API | FastAPI (Python) |
| Database / Auth / Storage | Supabase (Postgres, RLS, Auth, Vault) |
| Vector Search | Pinecone |
| Embeddings / LLM | OpenAI |
| Messaging | WhatsApp (WaSender API) |
| Email | Resend |
| Deployment | GitHub Actions, pm2 |

## Project Structure

```
app/
├── main.py            # FastAPI entry point, router registration
├── config.py          # Settings loaded from environment
├── middleware/        # JWT auth
├── models/            # Pydantic schemas
├── routes/            # API endpoints (auth, chat, query, webhook, appointments, ...)
├── services/          # Ingestion, retrieval (RAG), chunking, integrations
└── utils/             # Scrapers, embeddings, Pinecone / Supabase clients
supabase/migrations/   # Database schema migrations
docs/                  # Architecture, schema, API and multi-tenant docs
scripts/               # Smoke tests
```

## Getting Started

```bash
git clone https://github.com/<owner>/<repo-name>.git
cd <repo-name>

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# create a .env with your own keys (OpenAI, Pinecone, Supabase, WaSender, Resend)
uvicorn app.main:app --reload --port 8000
```

Interactive API docs are available at `http://localhost:8000/docs`.

Apply the SQL files in `supabase/migrations/` in order to set up the database.

## Documentation

See the [`docs/`](./docs) folder: [Architecture](./docs/ARCHITECTURE.md) · [Database Schema](./docs/DATABASE_SCHEMA.md) · [API Endpoints](./docs/API_ENDPOINTS.md) · [Multi-Tenant Design](./docs/MULTI_TENANT.md) · [PRD](./docs/PRD.md)

## Notes

- All credentials are read from environment variables; no secrets are committed.
- Domains, emails and tenant names in configuration and docs are generic placeholders.
