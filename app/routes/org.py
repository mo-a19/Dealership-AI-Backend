import asyncio
import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.middleware.auth import CurrentUser, get_current_user, require_role
from app.utils.supabase_client import get_svc

router = APIRouter(tags=["Organization"])
logger = logging.getLogger(__name__)


class FAQ(BaseModel):
    question: str
    answer: str


class ServiceField(BaseModel):
    name: str                                    # slug used internally, e.g. "damage_photos"
    label: str                                   # what the AI asks the customer
    type: Literal["text", "media"] = "text"      # text → typed answer; media → photo or video
    required: bool = True                        # if False, AI collects it but won't block appointment creation


class OrgService(BaseModel):
    type: str                         # garage-defined slug, e.g. "body_repair", "test_drive"
    label: str                        # display name shown to customer
    auto_schedule: bool = False       # True → send booking_url; False → AI collects, team calls back
    booking_url: Optional[str] = None
    enabled: bool = True
    estimated_duration: Optional[str] = None   # e.g. "24 hours", "3-5 business days" — AI tells this to customer
    required_fields: list[ServiceField] = []   # ordered checklist; mix of text and media fields
    faq: list[FAQ] = []                        # per-service Q&A injected into AI context


_DEFAULT_OPEN  = "09:00"
_DEFAULT_CLOSE = "18:00"


class DayHours(BaseModel):
    enabled: bool = True
    open: str = _DEFAULT_OPEN
    close: str = _DEFAULT_CLOSE


class BusinessHours(BaseModel):
    mon: DayHours = Field(default_factory=DayHours)
    tue: DayHours = Field(default_factory=DayHours)
    wed: DayHours = Field(default_factory=DayHours)
    thu: DayHours = Field(default_factory=DayHours)
    fri: DayHours = Field(default_factory=DayHours)
    sat: DayHours = Field(default_factory=lambda: DayHours(enabled=False))
    sun: DayHours = Field(default_factory=lambda: DayHours(enabled=False))


class OrgSettings(BaseModel):
    system_prompt: str | None = None
    llm_model: str | None = None
    language: str | None = None


@router.get("/org/settings", response_model=OrgSettings, summary="Get your org's AI settings")
async def get_org_settings(
    user: CurrentUser = Depends(get_current_user),
) -> OrgSettings:
    svc = get_svc()
    result = await asyncio.to_thread(
        lambda: svc.table("organizations")
        .select("settings")
        .eq("id", user.org_id)
        .maybe_single()
        .execute()
    )
    if not result.data:
        raise HTTPException(404, "Organization not found.")
    settings = result.data.get("settings") or {}
    return OrgSettings(
        system_prompt=settings.get("system_prompt"),
        llm_model=settings.get("llm_model"),
        language=settings.get("language"),
    )


@router.patch("/org/settings", response_model=OrgSettings, summary="Update your org's AI settings")
async def update_org_settings(
    body: OrgSettings,
    user: CurrentUser = require_role("admin"),
) -> OrgSettings:
    svc = get_svc()

    # Fetch current settings first so we only overwrite provided fields
    current = await asyncio.to_thread(
        lambda: svc.table("organizations")
        .select("settings")
        .eq("id", user.org_id)
        .maybe_single()
        .execute()
    )
    if not current.data:
        raise HTTPException(404, "Organization not found.")

    merged = dict(current.data.get("settings") or {})
    patch = body.model_dump(exclude_none=True)
    merged.update(patch)

    await asyncio.to_thread(
        lambda: svc.table("organizations")
        .update({"settings": merged})
        .eq("id", user.org_id)
        .execute()
    )
    logger.info("Org settings updated | org=%s | fields=%s", user.org_id, list(patch.keys()))
    return OrgSettings(**merged)


@router.get("/org/business-hours", response_model=BusinessHours)
async def get_business_hours(user: CurrentUser = Depends(get_current_user)) -> BusinessHours:
    svc = get_svc()
    result = await asyncio.to_thread(
        lambda: svc.table("organizations")
        .select("business_hours")
        .eq("id", user.org_id)
        .maybe_single()
        .execute()
    )
    if not result.data:
        raise HTTPException(404, "Organization not found.")
    return BusinessHours(**(result.data.get("business_hours") or {}))


@router.patch("/org/business-hours", response_model=BusinessHours)
async def update_business_hours(
    body: BusinessHours,
    user: CurrentUser = require_role("admin"),
) -> BusinessHours:
    svc = get_svc()
    await asyncio.to_thread(
        lambda: svc.table("organizations")
        .update({"business_hours": body.model_dump()})
        .eq("id", user.org_id)
        .execute()
    )
    logger.info("Business hours updated | org=%s", user.org_id)
    return body


@router.get("/org/services", response_model=list[OrgService])
async def get_services(user: CurrentUser = Depends(get_current_user)) -> list[OrgService]:
    svc = get_svc()
    result = await asyncio.to_thread(
        lambda: svc.table("organizations")
        .select("services")
        .eq("id", user.org_id)
        .maybe_single()
        .execute()
    )
    if not result.data:
        raise HTTPException(404, "Organization not found.")
    return [OrgService(**s) for s in (result.data.get("services") or [])]


@router.put("/org/services", response_model=list[OrgService])
async def update_services(
    body: list[OrgService],
    user: CurrentUser = require_role("admin"),
) -> list[OrgService]:
    svc = get_svc()
    await asyncio.to_thread(
        lambda: svc.table("organizations")
        .update({"services": [s.model_dump() for s in body]})
        .eq("id", user.org_id)
        .execute()
    )
    logger.info("Services updated | org=%s | count=%d", user.org_id, len(body))
    return body
