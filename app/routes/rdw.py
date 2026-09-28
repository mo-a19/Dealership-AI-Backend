"""RDW — Dutch vehicle registry lookup by licence plate.

Combines two public RDW open data endpoints (no auth needed):
  m9d7-ebf2 → vehicle basics (make, model, year, fuel type)
  sgfe-77wx → APK (MOT) expiry date
"""
import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.middleware.auth import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)

_RDW_BASE = "https://opendata.rdw.nl/resource"


@router.get("/rdw/vehicle/{plate}", summary="Dutch vehicle lookup by licence plate (RDW)")
async def rdw_vehicle(
    plate: str,
    _=Depends(get_current_user),
) -> dict:
    clean = plate.replace("-", "").upper()
    async with httpx.AsyncClient(timeout=10) as client:
        basics_r, apk_r = await asyncio.gather(
            client.get(f"{_RDW_BASE}/m9d7-ebf2.json", params={"kenteken": clean}),
            client.get(f"{_RDW_BASE}/sgfe-77wx.json", params={"kenteken": clean}),
        )
    basics_r.raise_for_status()
    apk_r.raise_for_status()

    basics = basics_r.json()
    if not basics:
        raise HTTPException(404, f"No vehicle found for plate '{clean}'.")

    v = basics[0]
    apk = apk_r.json()
    apk_raw = (apk[0].get("vervaldatum_apk") or apk[0].get("vervaldatum_apk_dt")) if apk else None

    result = {
        "plate": clean,
        "make": v.get("merk"),
        "model": v.get("handelsbenaming"),
        "year": (v.get("datum_eerste_toelating") or "")[:4] or None,
        "fuel": v.get("brandstof_omschrijving"),
        "colour": v.get("eerste_kleur"),
        "body": v.get("inrichting"),
        "apk_expiry": apk_raw[:10] if apk_raw else None,
    }
    logger.info("RDW lookup | plate=%s | make=%s model=%s", clean, result["make"], result["model"])
    return result
