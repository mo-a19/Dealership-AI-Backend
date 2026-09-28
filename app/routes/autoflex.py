"""Autoflex endpoints: connect / status (JWT-protected) and sync (secret-protected).

The sync endpoint lives on cron_router, which is registered in main.py WITHOUT the
global JWT dependency so an external scheduler can call it with a shared secret.
"""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.middleware.auth import CurrentUser, get_current_user, require_role
from app.services.autoflex import AutoflexClient, AutoflexError, build_org_client
from app.services.vehicle_ingestion import sync_running, sync_vehicles
from app.utils.supabase_client import get_svc, vault_write

router = APIRouter(tags=["Autoflex"])
cron_router = APIRouter(tags=["Autoflex"])
logger = logging.getLogger(__name__)


class AutoflexConnect(BaseModel):
    api_key: str
    username: str
    password: str
    api_url: str | None = None  # bootstrap entrypoint; defaults to server config


class AutoflexStatus(BaseModel):
    enabled: bool
    api_url: str | None = None
    last_sync_at: str | None = None


@router.post("/autoflex/connect", summary="Connect this dealership's Autoflex account")
async def connect(body: AutoflexConnect, user: CurrentUser = require_role("admin")) -> dict:
    bootstrap = (body.api_url or get_settings().autoflex_api_url or "").strip()
    if not bootstrap:
        raise HTTPException(503, "Autoflex entrypoint URL is not configured on the server.")

    # 1. Validate the credentials with a real login (also resolves the authoritative
    #    api_url). Nothing is stored unless this succeeds.
    client = AutoflexClient(
        api_key=body.api_key,
        username=body.username,
        password=body.password,
        base_url=bootstrap,
    )
    try:
        info = await client.authenticate()
    except AutoflexError as exc:
        raise HTTPException(400, f"Autoflex login failed: {exc}") from exc
    except Exception as exc:
        raise HTTPException(502, f"Could not reach Autoflex: {exc}") from exc

    # 2. Store secrets in Vault (idempotent on name), keep only the ids on the org row.
    org_id = user.org_id
    api_key_id = await vault_write(f"autoflex_apikey_{org_id}", body.api_key)
    username_id = await vault_write(f"autoflex_user_{org_id}", body.username)
    password_id = await vault_write(f"autoflex_pass_{org_id}", body.password)

    svc = get_svc()
    await asyncio.to_thread(
        lambda: svc.table("organizations")
        .update({
            "autoflex_api_key_id": api_key_id,
            "autoflex_username_id": username_id,
            "autoflex_password_id": password_id,
            "autoflex_api_url": info["api_url"],
            "autoflex_enabled": True,
        })
        .eq("id", org_id)
        .execute()
    )
    logger.info("Autoflex connected | org=%s | api_url=%s", org_id, info["api_url"])
    return {"connected": True, "api_url": info["api_url"]}


@router.get("/autoflex/status", response_model=AutoflexStatus, summary="Autoflex connection status")
async def status(user: CurrentUser = Depends(get_current_user)) -> AutoflexStatus:
    svc = get_svc()
    org = await asyncio.to_thread(
        lambda: svc.table("organizations")
        .select("autoflex_enabled, autoflex_api_url, autoflex_last_sync_at")
        .eq("id", user.org_id)
        .maybe_single()
        .execute()
    )
    if not org.data:
        raise HTTPException(404, "Organization not found.")
    d = org.data
    return AutoflexStatus(
        enabled=bool(d.get("autoflex_enabled")),
        api_url=d.get("autoflex_api_url"),
        last_sync_at=d.get("autoflex_last_sync_at"),
    )


# ---------------------------------------------------------------------------
# Cron-triggered sync endpoint  (issue #21)
# ---------------------------------------------------------------------------

class SyncTrigger(BaseModel):
    org_id: Optional[str] = None  # omit to sync all autoflex-enabled orgs


async def _enabled_org_ids() -> list[str]:
    svc = get_svc()
    result = await asyncio.to_thread(
        lambda: svc.table("organizations")
        .select("id")
        .eq("autoflex_enabled", True)
        .execute()
    )
    return [r["id"] for r in (result.data or [])]


async def _run_sync(org_id: str) -> None:
    sync_running.add(org_id)
    try:
        client = await build_org_client(org_id)
        await sync_vehicles(org_id, client)
    except Exception:
        logger.exception("Vehicle sync failed for org=%s", org_id)
    finally:
        sync_running.discard(org_id)


@cron_router.post(
    "/autoflex/sync",
    summary="Trigger vehicle catalog sync (cron / external scheduler)",
)
async def trigger_sync(
    body: SyncTrigger,
    background_tasks: BackgroundTasks,
    x_sync_secret: str | None = Header(default=None),
) -> dict:
    """Protected by X-Sync-Secret header. Safe to call repeatedly — already-running
    orgs are skipped. Omit org_id to sync all autoflex-enabled dealerships."""
    settings = get_settings()
    if not settings.autoflex_sync_secret:
        raise HTTPException(503, "AUTOFLEX_SYNC_SECRET is not configured on this server.")
    if x_sync_secret != settings.autoflex_sync_secret:
        raise HTTPException(401, "Invalid or missing X-Sync-Secret header.")

    org_ids = [body.org_id] if body.org_id else await _enabled_org_ids()

    started, already_running = [], []
    for oid in org_ids:
        if oid in sync_running:
            already_running.append(oid)
        else:
            sync_running.add(oid)  # mark eagerly — before task starts — to prevent double-trigger
            background_tasks.add_task(_run_sync, oid)
            started.append(oid)

    return {"started": started, "already_running": already_running}


@router.post("/autoflex/sync/trigger", summary="Manual sync trigger (JWT protected)")
async def manual_sync_trigger(
    background_tasks: BackgroundTasks,
    user: CurrentUser = require_role("admin"),
) -> dict:
    """Allows frontend to trigger a sync for their own org. No secret needed — JWT handles auth."""
    org_id = user.org_id
    if org_id in sync_running:
        return {"started": [], "already_running": [org_id]}
    sync_running.add(org_id)
    background_tasks.add_task(_run_sync, org_id)
    return {"started": [org_id], "already_running": []}


async def scheduled_sync_all() -> None:
    """Called by APScheduler every 4 hours — syncs all autoflex-enabled orgs."""
    org_ids = await _enabled_org_ids()
    if not org_ids:
        return
    for oid in org_ids:
        if oid not in sync_running:
            sync_running.add(oid)
            asyncio.create_task(_run_sync(oid))
    logger.info("Scheduled sync dispatched | orgs=%s", org_ids)
