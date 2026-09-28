import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.services import wasender
from app.utils.supabase_client import get_svc, vault_read

logger = logging.getLogger(__name__)

_DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


async def maybe_create_appointment(
    org: dict, lead_id: str | None, appt_type: str, notes: str = ""
) -> None:
    """Create a pending appointment. Intent is detected by the LLM, not keywords."""
    services = org.get("services") or []
    service = next((s for s in services if s.get("type") == appt_type and s.get("enabled", True)), None)
    if service and service.get("auto_schedule"):
        return  # external calendar (Google/Calendly) handles booking

    svc = get_svc()
    if lead_id:
        existing = await asyncio.to_thread(
            lambda: svc.table("appointments")
            .select("id")
            .eq("org_id", org["id"])
            .eq("lead_id", lead_id)
            .eq("type", appt_type)
            .in_("status", ["pending", "confirmed"])
            .execute()
        )
        if existing.data:
            return  # already has an open appointment of this type

    row: dict = {
        "org_id": org["id"],
        "type": appt_type,
        "scheduled_at": "2099-01-01T00:00:00+00:00",
        "status": "pending",
        "notes": notes[:500] if notes else "",
    }
    if lead_id:
        row["lead_id"] = lead_id

    try:
        await asyncio.to_thread(lambda: svc.table("appointments").insert(row).execute())
    except Exception:
        logger.exception("Appointment insert failed | org=%s type=%s lead=%s row=%s", org["id"], appt_type, lead_id, row)
        return
    await dashboard_notify(org["id"], lead_id, "appointment_created")
    logger.info("Appointment created | org=%s type=%s lead=%s", org["id"], appt_type, lead_id)

_STATUS_MESSAGES = {
    "confirmed": "Your appointment is confirmed. We look forward to seeing you! 🚗",
    "completed":  "Thank you for your visit. Hope to see you again soon!",
    "cancelled":  "Your appointment has been cancelled. Feel free to book again anytime.",
}


def is_within_business_hours(scheduled_at: datetime, business_hours: dict) -> bool:
    if not business_hours:
        return True
    day_cfg = business_hours.get(_DAY_KEYS[scheduled_at.weekday()], {})
    if not day_cfg.get("enabled", True):
        return False

    def _mins(t: str) -> int:
        h, m = map(int, t.split(":"))
        return h * 60 + m

    slot = scheduled_at.hour * 60 + scheduled_at.minute
    return _mins(day_cfg.get("open", "09:00")) <= slot < _mins(day_cfg.get("close", "18:00"))


async def whatsapp_notify(org_id: str, phone: str, message: str) -> None:
    svc = get_svc()
    org = await asyncio.to_thread(
        lambda: svc.table("organizations")
        .select("wasender_api_key_id")
        .eq("id", org_id)
        .maybe_single()
        .execute()
    )
    api_key_id = (org.data or {}).get("wasender_api_key_id")
    api_key = await vault_read(api_key_id) if api_key_id else None
    if not api_key:
        logger.warning("No WaSender API key for org=%s — WhatsApp notify skipped", org_id)
        return
    try:
        await wasender.send_text(api_key, phone, message)
    except Exception:
        logger.exception("WhatsApp notify failed | org=%s phone=%s", org_id, phone)


async def dashboard_notify(org_id: str, lead_id: str | None, ntype: str) -> None:
    try:
        await asyncio.to_thread(
            lambda: get_svc().table("notifications").insert({
                "org_id": org_id,
                "lead_id": lead_id,
                "type": ntype,
                "channel": "dashboard",
            }).execute()
        )
    except Exception:
        logger.warning("dashboard_notify failed | org=%s type=%s", org_id, ntype)


async def notify_status_change(
    org_id: str, lead_id: str | None, phone: str | None, new_status: str,
) -> None:
    msg = _STATUS_MESSAGES.get(new_status)
    if msg and phone:
        await whatsapp_notify(org_id, phone, msg)
    await dashboard_notify(org_id, lead_id, f"appointment_{new_status}")


async def send_appointment_reminders() -> None:
    """Run hourly. Sends 24h and 1h WhatsApp reminders for confirmed appointments."""
    svc = get_svc()
    now = datetime.now(timezone.utc)

    windows = [
        (now + timedelta(hours=23), now + timedelta(hours=25), "reminder_sent_24h", "tomorrow"),
        (now + timedelta(minutes=45), now + timedelta(minutes=75), "reminder_sent_1h", "in 1 hour"),
    ]

    for win_start, win_end, flag, when_label in windows:
        rows = await asyncio.to_thread(
            lambda ws=win_start, we=win_end, f=flag: svc.table("appointments")
            .select("id, org_id, lead_id, type, scheduled_at, leads(phone, name)")
            .eq("status", "confirmed")
            .eq(f, False)
            .gte("scheduled_at", ws.isoformat())
            .lte("scheduled_at", we.isoformat())
            .execute()
        )
        for appt in rows.data or []:
            lead = appt.get("leads") or {}
            phone = lead.get("phone") if isinstance(lead, dict) else None
            name = (lead.get("name") if isinstance(lead, dict) else None) or "there"
            if phone:
                dt = datetime.fromisoformat(appt["scheduled_at"]).strftime("%A, %d %B at %H:%M")
                label = appt["type"].replace("_", " ")
                await whatsapp_notify(
                    appt["org_id"], phone,
                    f"Hi {name}! Reminder: your {label} appointment is {when_label} ({dt}). See you soon! 🚗",
                )
            await asyncio.to_thread(
                lambda aid=appt["id"], f=flag: svc.table("appointments")
                .update({f: True})
                .eq("id", aid)
                .execute()
            )
            logger.info("Reminder sent | appt=%s flag=%s", appt["id"], flag)
