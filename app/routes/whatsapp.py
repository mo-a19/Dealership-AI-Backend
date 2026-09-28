"""Admin-facing WhatsApp endpoints: connect (QR), status, disconnect, and human
takeover. All protected by JWT; mutating actions require the 'admin' role.

QR durability: the WhatsApp socket lives on WaSender's infra. We persist the
session id + API key, so our restarts never require a re-scan — `connect` returns
'connected' (no QR) whenever the session is already live.
"""
import asyncio
import logging
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.config import get_settings
from app.middleware.auth import CurrentUser, get_current_user, require_role

from app.models.schemas import (
    AgentReply,
    ConversationMessages,
    ConversationModeUpdate,
    ConversationSummary,
    MessageItem,
    WhatsAppConnect,
    WhatsAppConnectResponse,
    WhatsAppStatus,
)
from app.services import wasender
from app.utils.supabase_client import get_svc, vault_read, vault_write

router = APIRouter(tags=["WhatsApp"])
logger = logging.getLogger(__name__)


async def _assert_webhook(session_id: int, public_base_url: str) -> None:
    """Best-effort: register our webhook URL + events with WaSender. The secret is
    WaSender-owned and learned on first delivery, so we don't set it here."""
    webhook_url = f"{public_base_url.rstrip('/')}/api/v1/webhook/wasender/{session_id}"
    try:
        await wasender.update_session(
            session_id,
            webhook_url=webhook_url,
            webhook_enabled=True,
            webhook_events=["messages.upsert", "session.status"],
        )
        logger.info("Asserted webhook config for session %s → %s", session_id, webhook_url)
    except Exception:
        logger.exception("Failed to assert webhook config for session %s", session_id)


async def _sync_credentials(org_id: str, detail: dict) -> None:
    """Persist WaSender-owned credentials (API key, webhook secret) from a session
    detail payload into Vault and the org row. Idempotent on secret name, so it's
    safe to call on every connect — keeps us in sync if WaSender rotates them."""
    update: dict = {}
    api_key = detail.get("api_key")
    if api_key:
        update["wasender_api_key_id"] = await vault_write(f"wasender_apikey_{org_id}", api_key)
    secret = detail.get("webhook_secret")
    if secret:
        update["wasender_webhook_secret_id"] = await vault_write(f"wasender_whsecret_{org_id}", secret)
    if update:
        svc = get_svc()
        await asyncio.to_thread(
            lambda: svc.table("organizations").update(update).eq("id", org_id).execute()
        )
        logger.info("Synced WaSender credentials for org=%s: %s", org_id, list(update.keys()))


@router.post("/whatsapp/connect", response_model=WhatsAppConnectResponse, summary="Connect WhatsApp / get QR")
async def connect(body: WhatsAppConnect, user: CurrentUser = require_role("admin")) -> WhatsAppConnectResponse:
    s = get_settings()
    if not s.wasender_pat or not s.public_base_url:
        raise HTTPException(503, "WhatsApp integration is not configured on the server.")

    svc = get_svc()
    org = await asyncio.to_thread(
        lambda: svc.table("organizations")
        .select("id, name, wasender_session_id, wasender_webhook_secret_id")
        .eq("id", user.org_id)
        .maybe_single()
        .execute()
    )
    if not org.data:
        raise HTTPException(404, "Organization not found.")
    org = org.data
    session_id = org.get("wasender_session_id")

    # If a session_id is stored, verify it still exists in WaSender.
    # Free-tier accounts can have sessions deleted externally; treat 404 as no session.
    if session_id:
        try:
            await wasender.get_session(session_id)
        except Exception:
            logger.warning("Stored session %s no longer exists in WaSender — resetting", session_id)
            await asyncio.to_thread(
                lambda: svc.table("organizations")
                .update({"wasender_session_id": None, "wasender_status": "disconnected"})
                .eq("id", org["id"])
                .execute()
            )
            session_id = None
        else:
            # Session alive — re-assert webhook config (best-effort). WaSender may
            # require this to be set in its dashboard; the webhook secret is owned by
            # WaSender and learned on first delivery by the webhook handler.
            await _assert_webhook(session_id, s.public_base_url)

    # First-time connect (or after stale reset): create/reuse the WaSender session.
    if not session_id:
        try:
            created = await wasender.create_session(org["name"], body.phone_number)
        except httpx.HTTPStatusError as exc:
            detail_text = exc.response.text[:200]
            raise HTTPException(502, f"WhatsApp service rejected the request: {detail_text}") from exc
        except httpx.TimeoutException:
            raise HTTPException(504, "WhatsApp service timed out. Please try again.")
        except Exception as exc:
            raise HTTPException(502, f"Failed to create WhatsApp session: {exc}") from exc

        session_id = created["id"]
        await _assert_webhook(session_id, s.public_base_url)
        await asyncio.to_thread(
            lambda: svc.table("organizations")
            .update(
                {
                    "wasender_session_id": session_id,
                    "wasender_status": "connecting",
                }
            )
            .eq("id", org["id"])
            .execute()
        )

    # Capture WaSender-owned credentials (API key + webhook secret) automatically.
    try:
        detail = await wasender.get_session(session_id)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"Failed to fetch WhatsApp session details: {exc.response.text[:200]}") from exc
    except httpx.TimeoutException:
        raise HTTPException(504, "WhatsApp service timed out while fetching session. Please try again.")
    except Exception as exc:
        raise HTTPException(502, f"Failed to fetch WhatsApp session details: {exc}") from exc

    await _sync_credentials(org["id"], detail)
    if detail.get("status") == "connected":
        return WhatsAppConnectResponse(session_id=session_id, status="connected")

    try:
        await wasender.connect_session(session_id)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"WhatsApp service failed to start connection: {exc.response.text[:200]}") from exc
    except httpx.TimeoutException:
        raise HTTPException(504, "WhatsApp service timed out while connecting. Please try again.")
    except Exception as exc:
        raise HTTPException(502, f"Failed to connect WhatsApp session: {exc}") from exc

    try:
        qr = await wasender.get_qr(session_id)
    except Exception:
        logger.warning("QR fetch failed for session %s — returning connecting status without QR", session_id)
        qr = ""
    return WhatsAppConnectResponse(session_id=session_id, status="connecting", qr=qr or None)


@router.get("/whatsapp/status", response_model=WhatsAppStatus, summary="WhatsApp connection status")
async def status(user: CurrentUser = Depends(get_current_user)) -> WhatsAppStatus:
    svc = get_svc()
    org = await asyncio.to_thread(
        lambda: svc.table("organizations")
        .select("wasender_session_id, wasender_status")
        .eq("id", user.org_id)
        .maybe_single()
        .execute()
    )
    if not org.data:
        raise HTTPException(404, "Organization not found.")

    sid = org.data.get("wasender_session_id")
    live_status = org.data.get("wasender_status")
    phone = None
    if sid and get_settings().wasender_pat:
        try:
            detail = await wasender.get_session(sid)
            live_status = detail.get("status") or live_status
            phone = detail.get("phone_number")
        except Exception:
            logger.exception("Live status fetch failed for session %s", sid)
    return WhatsAppStatus(status=live_status or "disconnected", session_id=sid, phone_number=phone)


@router.delete("/whatsapp/session", summary="Force-delete a WaSender session by phone number (admin debug)")
async def force_delete_session(phone_number: str, user: CurrentUser = require_role("admin")) -> dict:
    """Finds a WaSender session by phone number and permanently deletes it.
    Use this to clear ghost sessions that appear as 'already taken' but aren't
    visible in the WaSender dashboard."""
    if not get_settings().wasender_pat:
        raise HTTPException(503, "WhatsApp integration is not configured on the server.")

    sessions = await wasender.list_sessions()
    digits = "".join(c for c in phone_number if c.isdigit())
    match = next(
        (s for s in sessions if "".join(c for c in (s.get("phone_number") or "") if c.isdigit()) == digits),
        None,
    )
    if not match:
        raise HTTPException(404, f"No WaSender session found for phone number {phone_number!r}.")

    session_id = match["id"]
    await wasender.delete_session(session_id)
    logger.info("Force-deleted WaSender session %s for phone %s | requested by org=%s", session_id, digits, user.org_id)

    # Also clear from Supabase if it matches this org's stored session.
    svc = get_svc()
    org = await asyncio.to_thread(
        lambda: svc.table("organizations")
        .select("wasender_session_id")
        .eq("id", user.org_id)
        .maybe_single()
        .execute()
    )
    if org.data and org.data.get("wasender_session_id") == session_id:
        await asyncio.to_thread(
            lambda: svc.table("organizations")
            .update({"wasender_session_id": None, "wasender_status": "disconnected"})
            .eq("id", user.org_id)
            .execute()
        )
    return {"deleted": True, "session_id": session_id, "phone_number": match.get("phone_number")}


@router.delete("/whatsapp/disconnect", summary="Disconnect WhatsApp session")
async def disconnect(user: CurrentUser = require_role("admin")) -> dict:
    svc = get_svc()
    org = await asyncio.to_thread(
        lambda: svc.table("organizations")
        .select("wasender_session_id")
        .eq("id", user.org_id)
        .maybe_single()
        .execute()
    )
    sid = org.data.get("wasender_session_id") if org.data else None
    if sid and get_settings().wasender_pat:
        try:
            await wasender.disconnect_session(sid)
        except Exception:
            logger.exception("WaSender disconnect failed for session %s", sid)
    await asyncio.to_thread(
        lambda: svc.table("organizations")
        .update({"wasender_status": "disconnected"})
        .eq("id", user.org_id)
        .execute()
    )
    return {"disconnected": True}


# --- Conversations ---

@router.get("/whatsapp/conversations", response_model=list[ConversationSummary], summary="List all conversations for this org")
async def list_conversations(user: CurrentUser = Depends(get_current_user)) -> list[ConversationSummary]:
    svc = get_svc()
    result = await asyncio.to_thread(
        lambda: svc.table("conversations")
        .select("id, mode, assigned_to, created_at, leads(phone, name)")
        .eq("org_id", user.org_id)
        .order("created_at", desc=True)
        .execute()
    )
    return [
        ConversationSummary(
            id=row["id"],
            lead_phone=(row.get("leads") or {}).get("phone"),
            lead_name=(row.get("leads") or {}).get("name"),
            mode=row.get("mode") or "ai",
            assigned_to=row.get("assigned_to"),
            created_at=row["created_at"],
        )
        for row in (result.data or [])
    ]


@router.get("/whatsapp/conversations/{conversation_id}/messages", response_model=ConversationMessages, summary="Get all messages for a conversation")
async def get_conversation_messages(
    conversation_id: UUID,
    user: CurrentUser = Depends(get_current_user),
) -> ConversationMessages:
    svc = get_svc()
    conv = await asyncio.to_thread(
        lambda: svc.table("conversations")
        .select("id, mode, assigned_to, created_at, leads(phone, name)")
        .eq("id", str(conversation_id))
        .eq("org_id", user.org_id)
        .maybe_single()
        .execute()
    )
    if not conv.data:
        raise HTTPException(404, "Conversation not found.")

    msgs = await asyncio.to_thread(
        lambda: svc.table("messages")
        .select("id, role, content, created_at, media_url, media_type")
        .eq("conversation_id", str(conversation_id))
        .order("created_at")
        .execute()
    )
    lead = conv.data.get("leads") or {}
    return ConversationMessages(
        id=conv.data["id"],
        lead_phone=lead.get("phone"),
        lead_name=lead.get("name"),
        mode=conv.data.get("mode") or "ai",
        messages=[MessageItem(**m) for m in (msgs.data or [])],
        created_at=conv.data["created_at"],
    )


# --- Human takeover ---

@router.patch("/whatsapp/conversations/{conversation_id}", summary="Switch a conversation between AI and human")
async def set_mode(
    conversation_id: UUID,
    body: ConversationModeUpdate,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    svc = get_svc()
    update: dict = {"mode": body.mode}
    assigned_to = str(body.assigned_to) if body.assigned_to else None

    if body.mode == "human":
        if not assigned_to:
            assigned_to = user.user_id
        update["assigned_to"] = assigned_to
    elif body.mode == "ai":
        update["assigned_to"] = None

    res = await asyncio.to_thread(
        lambda: svc.table("conversations")
        .update(update)
        .eq("id", str(conversation_id))
        .eq("org_id", user.org_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Conversation not found.")
    logger.info("Conversation %s → mode=%s assigned_to=%s | org=%s", conversation_id, body.mode, assigned_to, user.org_id)
    return {"conversation_id": str(conversation_id), "mode": body.mode, "assigned_to": assigned_to}


@router.post("/whatsapp/conversations/{conversation_id}/messages", summary="Agent sends a WhatsApp reply")
async def agent_reply(
    conversation_id: UUID,
    body: AgentReply,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    if not body.content.strip():
        raise HTTPException(422, "content must not be empty.")
    svc = get_svc()

    conv = await asyncio.to_thread(
        lambda: svc.table("conversations")
        .select("id, wa_thread_id, mode, assigned_to, leads(phone)")
        .eq("id", str(conversation_id))
        .eq("org_id", user.org_id)
        .maybe_single()
        .execute()
    )
    if not conv.data:
        raise HTTPException(404, "Conversation not found.")

    # Require mode='human' and this agent is assigned
    if conv.data.get("mode") != "human":
        raise HTTPException(409, "Conversation is in AI mode — cannot send agent reply.")
    if conv.data.get("assigned_to") and conv.data["assigned_to"] != user.user_id:
        raise HTTPException(403, "Conversation is assigned to another agent.")

    org = await asyncio.to_thread(
        lambda: svc.table("organizations")
        .select("wasender_api_key_id, wasender_session_id")
        .eq("id", user.org_id)
        .maybe_single()
        .execute()
    )
    api_key_id = org.data.get("wasender_api_key_id") if org.data else None
    api_key = await vault_read(api_key_id) if api_key_id else None
    if not api_key:
        raise HTTPException(409, "WhatsApp is not connected for this organization.")

    await asyncio.to_thread(
        lambda: svc.table("messages")
        .insert(
            {
                "conversation_id": str(conversation_id),
                "org_id": str(user.org_id),
                "role": "agent",
                "content": body.content,
            }
        )
        .execute()
    )

    lead_phone = (conv.data.get("leads") or {}).get("phone", "")
    if not lead_phone:
        raise HTTPException(409, "Conversation has no phone number — cannot send reply.")
    digits = "".join(c for c in lead_phone if c.isdigit())
    to = f"+{digits}" if digits else ""
    if not to:
        raise HTTPException(409, "Conversation has no phone number — cannot send reply.")
    try:
        await wasender.send_text(api_key, to, body.content)
    except httpx.ConnectError as exc:
        raise HTTPException(503, "Cannot reach WhatsApp gateway — check server network or try again.") from exc
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 401:
            raise HTTPException(
                502,
                f"WhatsApp delivery failed ({exc.response.status_code}): {exc.response.text[:300]}",
            ) from exc
        # Stale API key — refresh from WaSender and retry once
        session_id = org.data.get("wasender_session_id")
        if not session_id:
            raise HTTPException(502, "WhatsApp session not found — reconnect via settings.") from exc
        try:
            detail = await wasender.get_session(session_id)
            fresh_key = detail.get("api_key")
            if not fresh_key:
                raise HTTPException(502, "Could not refresh WhatsApp API key — reconnect via settings.") from exc
            new_key_id = await vault_write(f"wasender_apikey_{user.org_id}", fresh_key)
            await asyncio.to_thread(
                lambda: svc.table("organizations")
                .update({"wasender_api_key_id": new_key_id})
                .eq("id", user.org_id)
                .execute()
            )
            await wasender.send_text(fresh_key, to, body.content)
            logger.info("API key refreshed and message resent | org=%s", user.org_id)
        except (httpx.ConnectError, httpx.HTTPStatusError) as retry_exc:
            detail = retry_exc.response.text[:200] if isinstance(retry_exc, httpx.HTTPStatusError) else str(retry_exc)
            raise HTTPException(502, f"WhatsApp delivery failed after key refresh: {detail}") from retry_exc
    logger.info("Agent reply sent | conv=%s | org=%s", conversation_id, user.org_id)
    return {"conversation_id": conversation_id, "sent": True}
