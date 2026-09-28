import asyncio
import logging
from typing import Literal, Optional
from uuid import UUID

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from postgrest.exceptions import APIError
from pydantic import BaseModel

from app.middleware.auth import CurrentUser, get_current_user
from app.services.appointments import notify_status_change
from app.utils.supabase_client import get_svc

router = APIRouter(tags=["Appointments"])
logger = logging.getLogger(__name__)

_TRANSITIONS: dict[str, set] = {
    "pending":   {"confirmed", "cancelled"},
    "confirmed": {"completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


class AppointmentStatusUpdate(BaseModel):
    status: Literal["confirmed", "completed", "cancelled"]
    scheduled_at: Optional[datetime] = None  # required when confirming


@router.get("/appointments")
async def list_appointments(
    status: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    user: CurrentUser = Depends(get_current_user),
) -> list:
    svc = get_svc()
    q = (
        svc.table("appointments")
        .select("*, leads(name, phone)")
        .eq("org_id", user.org_id)
        .order("scheduled_at")
    )
    if status:
        q = q.eq("status", status.strip().lower())
    if type:
        q = q.eq("type", type.strip().lower().replace(" ", "_"))
    try:
        result = await asyncio.to_thread(lambda: q.execute())
    except APIError as e:
        raise HTTPException(422, detail=e.message)
    return result.data or []


@router.get("/appointments/{appointment_id}")
async def get_appointment(
    appointment_id: UUID,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    svc = get_svc()
    try:
        result = await asyncio.to_thread(
            lambda: svc.table("appointments")
            .select("*, leads(name, phone)")
            .eq("id", str(appointment_id))
            .eq("org_id", user.org_id)
            .maybe_single()
            .execute()
        )
    except APIError as e:
        raise HTTPException(422, detail=e.message)
    if not result.data:
        raise HTTPException(404, "Appointment not found.")
    return result.data


@router.patch("/appointments/{appointment_id}/status")
async def update_appointment_status(
    appointment_id: UUID,
    body: AppointmentStatusUpdate,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    svc = get_svc()
    current = await asyncio.to_thread(
        lambda: svc.table("appointments")
        .select("id, status, lead_id, leads(phone)")
        .eq("id", str(appointment_id))
        .eq("org_id", user.org_id)
        .maybe_single()
        .execute()
    )
    if not current.data:
        raise HTTPException(404, "Appointment not found.")

    old_status = current.data["status"]
    if body.status not in _TRANSITIONS.get(old_status, set()):
        raise HTTPException(422, f"Cannot move from '{old_status}' to '{body.status}'.")
    if body.status == "confirmed" and not body.scheduled_at:
        raise HTTPException(422, "scheduled_at is required when confirming an appointment.")

    update: dict = {"status": body.status}
    if body.scheduled_at:
        update["scheduled_at"] = body.scheduled_at.isoformat()

    try:
        result = await asyncio.to_thread(
            lambda: svc.table("appointments")
            .update(update)
            .eq("id", str(appointment_id))
            .execute()
        )
    except APIError as e:
        raise HTTPException(422, detail=e.message)

    lead = current.data.get("leads") or {}
    phone = lead.get("phone") if isinstance(lead, dict) else None
    lead_id = current.data.get("lead_id")

    await notify_status_change(user.org_id, lead_id, phone, body.status)
    logger.info("Appointment %s | %s → %s | org=%s", appointment_id, old_status, body.status, user.org_id)
    return result.data[0]
