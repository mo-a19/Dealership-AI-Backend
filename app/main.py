import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.middleware.auth import get_current_user
from app.routes import appointments, auth, autoflex, chat, org, pdf, query, rdw, scraper, system, webhook, whatsapp
from app.routes.autoflex import cron_router as autoflex_cron_router, scheduled_sync_all
from app.services import wasender
from app.services.appointments import send_appointment_reminders
from app.utils.pinecone_client import ensure_index

settings = get_settings()
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Dealership AI starting up…")
    await ensure_index()
    try:
        await wasender.reconnect_existing_sessions()
    except Exception:
        logger.exception("WhatsApp reconnect on startup failed (non-fatal)")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(scheduled_sync_all, "interval", hours=4)
    scheduler.add_job(send_appointment_reminders, "interval", hours=1)
    scheduler.start()
    logger.info("Schedulers started: autoflex sync (4h), appointment reminders (1h)")

    yield

    scheduler.shutdown(wait=False)
    logger.info("Dealership AI shut down.")


app = FastAPI(
    title="Dealership AI",
    description=(
        "Scrape any website and query it with maximum factual accuracy.\n\n"
        "**Protected endpoints** require a Supabase JWT. "
        "Click **Authorize** (top right), paste your `access_token`, and submit."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:3000",
        "http://localhost:5173",
        "https://app.example.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_auth = [Depends(get_current_user)]

# Public — no token required
app.include_router(auth.router,          prefix="/api/v1")
app.include_router(system.router,        prefix="/api/v1")
app.include_router(webhook.router,       prefix="/api/v1")  # WaSender posts here (signature-authed)
app.include_router(autoflex_cron_router, prefix="/api/v1")  # cron sync (X-Sync-Secret authed)

# Protected — valid JWT + org_id required
app.include_router(org.router,      prefix="/api/v1",                          dependencies=_auth)
app.include_router(scraper.router,  prefix="/api/v1", tags=["Ingestion"],      dependencies=_auth)
app.include_router(pdf.router,      prefix="/api/v1", tags=["Ingestion"],      dependencies=_auth)
app.include_router(query.router,    prefix="/api/v1", tags=["Search"],         dependencies=_auth)
app.include_router(chat.router,     prefix="/api/v1", tags=["Conversations"],  dependencies=_auth)
app.include_router(whatsapp.router,     prefix="/api/v1",                              dependencies=_auth)
app.include_router(autoflex.router,     prefix="/api/v1",                              dependencies=_auth)
app.include_router(appointments.router, prefix="/api/v1", tags=["Appointments"],       dependencies=_auth)
app.include_router(rdw.router,          prefix="/api/v1", tags=["RDW"],                dependencies=_auth)
