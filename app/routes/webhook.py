"""Public WaSender webhook — inbound WhatsApp messages and session-status events.

No JWT: the org is identified by the session_id in the URL path, and the request
is authenticated by the per-org X-Webhook-Signature secret stored in Vault.

Idempotency: messages are deduped on (org_id, wa_message_id) via ON CONFLICT DO
NOTHING — the AI reply fires only when the insert actually creates a row, so
retried or duplicate deliveries never produce a second reply.
"""
import asyncio
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.services import wasender
from app.services.appointments import maybe_create_appointment
from app.services.retrieval import retrieve_and_answer
from app.utils.supabase_client import get_svc, vault_read, vault_write

router = APIRouter(tags=["WhatsApp Webhook"])
logger = logging.getLogger(__name__)


async def _find_org(session_id: int) -> dict | None:
    svc = get_svc()
    res = await asyncio.to_thread(
        lambda: svc.table("organizations")
        .select("id, name, settings, services, wasender_session_id, wasender_api_key_id, wasender_webhook_secret_id")
        .eq("wasender_session_id", session_id)
        .maybe_single()
        .execute()
    )
    return res.data if res and res.data else None


@router.post("/webhook/wasender/{session_id}")
async def wasender_webhook(session_id: int, request: Request, background: BackgroundTasks) -> dict:
    payload = await request.json()
    event = payload.get("event", "")

    org = await _find_org(session_id)
    if not org:
        # Ack so WaSender stops retrying; nothing we can route.
        logger.warning("Webhook for unknown wasender_session_id=%s", session_id)
        return {"ok": True}

    # Authenticate. WaSender owns the webhook secret (auto-generated, configured in
    # its dashboard) and sends it verbatim as X-Webhook-Signature — it is NOT settable
    # via the API. Since we already route by the session_id in the URL path, we treat
    # WaSender's signature as the source of truth: learn it on first sight and persist
    # it, self-healing any stale/mismatched secret. Once stored, an exact match is
    # required, so spoofed calls with a wrong signature are still rejected.
    secret_id = org.get("wasender_webhook_secret_id")
    secret = await vault_read(secret_id) if secret_id else None
    sig = request.headers.get("X-Webhook-Signature")

    if not sig:
        logger.warning("Webhook with no X-Webhook-Signature | org=%s event=%s", org["id"], event)
    elif secret is None or not hmac.compare_digest(sig, secret):
        if secret is not None:
            logger.warning("Stored webhook secret stale for org=%s — relearning from WaSender", org["id"])
        new_id = await vault_write(f"wasender_whsecret_{org['id']}", sig)
        await asyncio.to_thread(
            lambda: get_svc().table("organizations")
            .update({"wasender_webhook_secret_id": new_id})
            .eq("id", org["id"])
            .execute()
        )
        logger.info("Stored WaSender webhook secret for org=%s", org["id"])

    if event == "session.status":
        await _handle_session_status(org, payload)
    elif event == "messages.upsert":
        await _handle_messages_upsert(org, payload, background)
    # other events are intentionally ignored

    return {"ok": True}


async def _handle_session_status(org: dict, payload: dict) -> None:
    """Persist connection state; capture the session API key once connected."""
    svc = get_svc()
    status = (payload.get("data") or {}).get("status", "")

    if status == "connected":
        detail = await wasender.get_session(org["wasender_session_id"])
        api_key = detail.get("api_key")
        update: dict = {"wasender_status": "connected"}
        if api_key:
            update["wasender_api_key_id"] = await vault_write(
                f"wasender_apikey_{org['id']}", api_key
            )
        await asyncio.to_thread(
            lambda: svc.table("organizations").update(update).eq("id", org["id"]).execute()
        )
    else:
        # need_scan → awaiting QR; disconnected → down. Both non-operational.
        new_status = "connecting" if status == "need_scan" else "disconnected"
        await asyncio.to_thread(
            lambda: svc.table("organizations")
            .update({"wasender_status": new_status})
            .eq("id", org["id"])
            .execute()
        )
    logger.info("Session status | org=%s | wasender=%s", org["id"], status)


async def _handle_messages_upsert(org: dict, payload: dict, background: BackgroundTasks) -> None:
    data = payload.get("data") or {}
    msgs = data.get("messages")
    if isinstance(msgs, dict):
        msgs = [msgs]
    elif not isinstance(msgs, list):
        msgs = []
    for m in msgs:
        await _process_message(org, m, background)


_MEDIA_TYPE_MAP = {
    # Baileys full names
    "imageMessage":    "image",
    "videoMessage":    "video",
    "documentMessage": "document",
    "audioMessage":    "audio",
    "stickerMessage":  "image",
    # WaSender short names
    "image":    "image",
    "video":    "video",
    "document": "document",
    "audio":    "audio",
}


def _detect_media_type(m: dict) -> str | None:
    """Detect media type from a WaSender message object."""
    # messageType field (Baileys) or type field (WaSender short form)
    kind = _MEDIA_TYPE_MAP.get(m.get("messageType") or m.get("type") or "")
    if kind:
        return kind
    # Fallback: scan message object keys — Baileys embeds the type as the key name
    for key in (m.get("message") or {}):
        kind = _MEDIA_TYPE_MAP.get(key)
        if kind:
            return kind
    return None


async def _process_message(org: dict, m: dict, background: BackgroundTasks) -> None:
    key = m.get("key") or {}
    if key.get("fromMe"):
        return  # outbound echo

    remote_jid = key.get("remoteJid") or ""
    if not remote_jid or remote_jid.endswith("@g.us") or remote_jid == "status@broadcast":
        return  # groups / status broadcasts are out of scope

    media_type = _detect_media_type(m)
    media_url: str | None = None

    if media_type:
        api_key_id = org.get("wasender_api_key_id")
        api_key = await vault_read(api_key_id) if api_key_id else None
        if api_key:
            media_url = await wasender.decrypt_media(api_key, m)
            if not media_url:
                logger.warning("Media decrypt returned no URL | org=%s type=%s", org["id"], media_type)
        else:
            logger.warning("No WaSender API key for media decrypt | org=%s", org["id"])

    text = m.get("messageBody") or (m.get("message") or {}).get("conversation") or ""

    # For media: use caption as text; fall back to a placeholder the AI can read
    if media_type and not text:
        text = f"[{media_type}]"
    if not text.strip():
        return  # truly empty — nothing to process

    wa_message_id = key.get("id")
    phone = (
        key.get("cleanedSenderPn")
        or (key.get("senderPn") or "").split("@")[0]
        or remote_jid.split("@")[0]
    )
    push_name = m.get("pushName")
    org_id = org["id"]
    svc = get_svc()

    # 1. Customer (lead) — find-or-create by (org_id, phone).
    lead_row = {"org_id": org_id, "phone": phone, "channel": "whatsapp"}
    if push_name:
        lead_row["name"] = push_name
    lead = await asyncio.to_thread(
        lambda: svc.table("leads").upsert(lead_row, on_conflict="org_id,phone").execute()
    )
    lead_id = lead.data[0]["id"]

    # 2. Conversation — one durable thread per (org_id, wa_thread_id).
    conv = await asyncio.to_thread(
        lambda: svc.table("conversations")
        .upsert(
            {"org_id": org_id, "lead_id": lead_id, "wa_thread_id": remote_jid},
            on_conflict="org_id,wa_thread_id",
        )
        .execute()
    )
    conv_id = conv.data[0]["id"]
    mode = conv.data[0]["mode"]

    # 3. Persist the inbound message — idempotent dedup on (org_id, wa_message_id).
    msg_row: dict = {
        "conversation_id": conv_id,
        "org_id": org_id,
        "role": "user",
        "content": text,
        "wa_message_id": wa_message_id,
    }
    if media_url:
        msg_row["media_url"] = media_url
    if media_type:
        msg_row["media_type"] = media_type
    inserted = await asyncio.to_thread(
        lambda: svc.table("messages")
        .upsert(msg_row, on_conflict="org_id,wa_message_id", ignore_duplicates=True)
        .execute()
    )
    if not inserted.data:
        logger.info("Duplicate WhatsApp message id=%s org=%s — skipped", wa_message_id, org_id)
        return
    current_msg_id = inserted.data[0]["id"]

    # 4. Branch. Human mode → agent handles it (delivered via Supabase Realtime).
    if mode == "human":
        logger.info("Conv %s in human mode — AI skipped", conv_id)
        return

    background.add_task(_generate_and_reply, org, conv_id, current_msg_id, text, phone, lead_id, push_name, media_type)


async def _generate_and_reply(
    org: dict, conv_id: str, current_msg_id: str, question: str, to: str,
    lead_id: str | None = None,
    customer_name: str | None = None,
    inbound_media_type: str | None = None,
) -> None:
    """Runs after the 200 ack: load history, answer via RAG, persist + send."""
    try:
        svc = get_svc()
        rows = await asyncio.to_thread(
            lambda: svc.table("messages")
            .select("role, content, media_type")
            .eq("conversation_id", conv_id)
            .neq("id", current_msg_id)
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        history = []
        for r in reversed(rows.data or []):
            content = r["content"] or ""
            if r.get("media_type"):
                content = f"[Customer sent a {r['media_type']}] {content}".strip()
            history.append({
                "role": "assistant" if r["role"] in ("assistant", "agent") else "user",
                "content": content,
            })

        # Enrich current message with media context so the AI knows what arrived
        if inbound_media_type:
            question = f"[Customer sent a {inbound_media_type}] {question}".strip()
        s = org.get("settings") or {}
        result = await retrieve_and_answer(
            question, org["id"],
            system_prompt=s.get("system_prompt") or None,
            language=s.get("language") or None,
            customer_name=customer_name,
            max_reply_tokens=600,
            history=history,
            services=org.get("services") or None,
        )

        await asyncio.to_thread(
            lambda: svc.table("messages")
            .insert({
                "conversation_id": conv_id,
                "org_id": org["id"],
                "role": "assistant",
                "content": result.answer,
                "sources_used": [s.model_dump() for s in result.sources],
            })
            .execute()
        )

        api_key_id = org.get("wasender_api_key_id")
        api_key = await vault_read(api_key_id) if api_key_id else None
        if not api_key:
            logger.error("No WaSender API key for org %s — reply not sent", org["id"])
            return
        await wasender.send_text(api_key, to, result.answer)
        logger.info("AI replied | org=%s conv=%s chunks=%d", org["id"], conv_id, result.retrieved_chunks)

        for intent in result.appointment_intents:
            await maybe_create_appointment(org, lead_id, intent.type, intent.notes or question)

    except Exception:
        logger.exception("AI reply failed | org=%s conv=%s", org.get("id"), conv_id)


