"""Vehicle knowledge sync for Autoflex (issues #19 – #20).

One Pinecone vector per published vehicle.
Vector id  : autoflex-{vehicle_id}  — stable; namespace = org_id for tenant isolation.
Content hash: unchanged vehicles are skipped so we never waste embeds.
Reconcile  : vectors for removed / unpublished vehicles are deleted from Pinecone and kb_chunks.

Field names verified against the Autoflex OpenAPI spec (413 KB spec, 237-field Vehicle model).
"""
import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

import tiktoken

from app.services.autoflex import AutoflexClient
from app.utils.embeddings import embed_texts
from app.utils.pinecone_client import get_index
from app.utils.supabase_client import get_svc

logger = logging.getLogger(__name__)

_EMBED_BATCH = 80
_UPSERT_BATCH = 100
_enc = tiktoken.get_encoding("cl100k_base")

# Customer-facing subset — field names taken directly from the Vehicle schema in the spec.
#   fuel_code       integer  Brandstof         → /util/list fuelcode
#   transmission_type integer Transmissie       → /util/list transmission
#   color           string   Kleur (label)      — already human-readable, no lookup needed
#   milometer       integer  Tellerstand        — always km
_VEHICLE_FIELDS = [
    "vehicle_id", "brand", "model", "modelyear", "v_display_name",
    "sell_price", "sell_price_take_away",
    "is_new_vehicle", "is_demo_vehicle", "is_sold", "is_archived",
    "fuel_code", "fuel_consumption_average",
    "transmission_type", "color",
    "number_of_doors", "number_of_seats", "capacity_hp",
    "milometer",
    "first_admission_date", "formatted_license_plate",
    "vehicle_description_valuelist",
    "is_tobe_published", "publish_status",
]

# Lock: org_ids whose sync is currently running (in-process, single-worker).
sync_running: set[str] = set()


# ---------------------------------------------------------------------------
# Lookup resolution  (#19)
# ---------------------------------------------------------------------------

# List names exactly as documented in spec field descriptions.
_UTIL_LISTS = ["fuelcode", "transmission"]


async def fetch_util_lookups(client: AutoflexClient) -> dict[str, dict[str, str]]:
    """Return {LISTNAME: {str(code): label}} from GET /util/list?list=<name>.

    Actual response shape: {'fuelcode': [{'description': 'Benzine', 'value': 1}, ...], 'count': 1}
    The list name is the top-level key; entries use 'value' (int) and 'description'.
    """
    lookup: dict[str, dict[str, str]] = {}
    for list_name in _UTIL_LISTS:
        try:
            result = await client.get("/util/list", params={"list": list_name})
            rows = result.get(list_name, []) if isinstance(result, dict) else (result or [])
            table: dict[str, str] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                code = str(row.get("value") or row.get("code") or "").strip()
                label = str(row.get("description") or row.get("omschrijving") or code)
                if code:
                    table[code] = label
            if table:
                lookup[list_name.upper()] = table
                logger.info("Loaded lookup '%s' (%d entries)", list_name, len(table))
        except Exception as exc:
            logger.warning("GET /util/list?list=%s failed — skipped: %s", list_name, exc)
    return lookup


def _resolve(lookups: dict, list_name: str, code: Any) -> str:
    if code is None:
        return ""
    s = str(code).strip()
    return lookups.get(list_name.upper(), {}).get(s, s) if s else ""


# ---------------------------------------------------------------------------
# Text builder  (#19)
# ---------------------------------------------------------------------------

def _fmt_price(val: Any) -> str:
    try:
        return f"€ {float(val):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return ""


def _fmt_date(val: Any) -> str:
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(val)[:10], fmt).strftime("%-d %B %Y")
        except (ValueError, TypeError):
            pass
    return str(val) if val else ""


def _vehicle_type(v: dict) -> str:
    if v.get("is_new_vehicle") in (1, True, "1"):
        return "New"
    if v.get("is_demo_vehicle") in (1, True, "1"):
        return "Demo"
    return "Used"


def build_vehicle_text(v: dict, lookups: dict[str, dict[str, str]]) -> str:
    """Return one natural-language text block for a single vehicle record."""
    display = (v.get("v_display_name") or "").strip() or " ".join(filter(None, [
        str(v.get("brand") or ""),
        str(v.get("model") or ""),
        str(v.get("modelyear") or ""),
    ]))

    vtype = _vehicle_type(v)
    price = _fmt_price(v.get("sell_price_take_away") or v.get("sell_price"))
    fuel = _resolve(lookups, "FUELCODE", v.get("fuel_code"))
    gearbox = _resolve(lookups, "TRANSMISSION", v.get("transmission_type"))
    color = (v.get("color") or "").strip()      # already a label string per the spec
    milometer = v.get("milometer")
    consumption = v.get("fuel_consumption_average")
    doors = v.get("number_of_doors")
    seats = v.get("number_of_seats")
    hp = v.get("capacity_hp")
    reg = _fmt_date(v.get("first_admission_date"))
    plate = (v.get("formatted_license_plate") or "").strip()
    description = (v.get("vehicle_description_valuelist") or "").strip()

    headline = display
    if vtype:
        headline += f" – {vtype}"
    if price:
        headline += f", {price}"

    parts = [headline]

    spec = ", ".join(filter(None, [
        fuel, gearbox, color,
        f"{doors}-door" if doors else "",
        f"{seats}-seat" if seats else "",
        f"{hp} hp" if hp else "",
    ]))
    if spec:
        parts.append(spec)

    detail_items = []
    if milometer:
        detail_items.append(f"Mileage: {int(float(milometer)):,} km".replace(",", "."))
    if consumption:
        detail_items.append(f"Fuel consumption: {consumption} l/100km")
    if reg:
        detail_items.append(f"First registered: {reg}")
    if plate:
        detail_items.append(f"License plate: {plate}")
    if detail_items:
        parts.append(". ".join(detail_items))

    if description:
        parts.append(description)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Sync pipeline  (#20)
# ---------------------------------------------------------------------------

def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _is_published(v: dict) -> bool:
    tp = v.get("is_tobe_published")
    if tp == 0:
        return False        # explicitly unpublished
    if tp == 1:
        return True         # explicitly published
    # tp is None (not set in this env) — fall back: include unless archived or sold
    if v.get("is_archived") == 1:
        return False
    if v.get("is_sold") == 1:
        return False
    return True


async def _get_or_create_autoflex_doc(org_id: str, source_url: str) -> str:
    svc = get_svc()
    result = await asyncio.to_thread(
        lambda: svc.table("kb_documents")
        .select("id")
        .eq("org_id", org_id)
        .eq("source_type", "autoflex")
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]["id"]

    doc = await asyncio.to_thread(
        lambda: svc.table("kb_documents")
        .insert({"org_id": org_id, "source_type": "autoflex", "source_url": source_url, "status": "processing"})
        .execute()
    )
    return doc.data[0]["id"]


async def _load_existing(org_id: str, doc_id: str) -> dict[str, str]:
    """Return {pinecone_vector_id: content_hash} for all known vehicle vectors."""
    svc = get_svc()
    rows = await asyncio.to_thread(
        lambda: svc.table("kb_chunks")
        .select("pinecone_vector_id, content_hash")
        .eq("org_id", org_id)
        .eq("doc_id", doc_id)
        .execute()
    )
    return {r["pinecone_vector_id"]: r.get("content_hash") or "" for r in (rows.data or [])}


async def sync_vehicles(org_id: str, client: AutoflexClient) -> dict:
    """Full vehicle sync for one dealership org. Returns summary stats."""
    svc = get_svc()
    index = get_index()
    synced_at = datetime.now(timezone.utc).isoformat()

    kb_doc_id = await _get_or_create_autoflex_doc(org_id, client._api_url)
    existing = await _load_existing(org_id, kb_doc_id)
    lookups = await fetch_util_lookups(client)

    raw = await client.get_all("/vehicle", fields=_VEHICLE_FIELDS)
    vehicles = [v for v in raw if v.get("vehicle_id") and _is_published(v)]
    logger.info("org=%s  published vehicles: %d", org_id, len(vehicles))

    to_embed: list[tuple[dict, str, str, str]] = []
    published_ids: set[str] = set()
    skipped = 0

    for v in vehicles:
        vid = f"autoflex-{v['vehicle_id']}"
        published_ids.add(vid)
        text = build_vehicle_text(v, lookups)
        h = _content_hash(text)
        if existing.get(vid) == h:
            skipped += 1
        else:
            to_embed.append((v, text, h, vid))

    indexed = 0
    for i in range(0, len(to_embed), _EMBED_BATCH):
        batch = to_embed[i: i + _EMBED_BATCH]
        vecs = await embed_texts([b[1] for b in batch])

        pinecone_records = []
        chunk_rows = []
        for (v, text, h, vid), vec in zip(batch, vecs):
            pinecone_records.append({
                "id": vid,
                "values": vec,
                "metadata": {
                    "org_id": org_id,
                    "source_type": "autoflex",
                    "url": f"autoflex://{v['vehicle_id']}",
                    "title": f"{v.get('brand', '')} {v.get('model', '')} {v.get('modelyear', '')}".strip(),
                    "section_path": "vehicles",
                    "text": text[:4000],
                    "synced_at": synced_at,
                },
            })
            chunk_rows.append({
                "org_id": org_id,
                "doc_id": kb_doc_id,
                "pinecone_vector_id": vid,
                "chunk_index": 0,
                "content_preview": text[:200],
                "content_hash": h,
                "token_count": len(_enc.encode(text)),
            })

        for j in range(0, len(pinecone_records), _UPSERT_BATCH):
            await asyncio.to_thread(
                index.upsert,
                vectors=pinecone_records[j: j + _UPSERT_BATCH],
                namespace=org_id,
            )
        await asyncio.to_thread(
            lambda rows=chunk_rows: svc.table("kb_chunks")
            .upsert(rows, on_conflict="pinecone_vector_id")
            .execute()
        )
        indexed += len(batch)
        logger.info("org=%s  embedded %d/%d", org_id, min(i + _EMBED_BATCH, len(to_embed)), len(to_embed))

    # Reconcile: delete vectors for vehicles no longer published
    stale = list(set(existing) - published_ids)
    deleted = len(stale)
    if stale:
        for i in range(0, len(stale), _UPSERT_BATCH):
            await asyncio.to_thread(
                index.delete, ids=stale[i: i + _UPSERT_BATCH], namespace=org_id
            )
        await asyncio.to_thread(
            lambda s=stale: svc.table("kb_chunks")
            .delete()
            .in_("pinecone_vector_id", s)
            .execute()
        )
        logger.info("org=%s  deleted %d stale vectors", org_id, deleted)

    await asyncio.to_thread(
        lambda: svc.table("kb_documents")
        .update({"status": "indexed", "chunk_count": len(vehicles)})
        .eq("id", kb_doc_id)
        .execute()
    )
    await asyncio.to_thread(
        lambda: svc.table("organizations")
        .update({"autoflex_last_sync_at": synced_at})
        .eq("id", org_id)
        .execute()
    )

    stats = {"vehicles_found": len(vehicles), "indexed": indexed, "skipped": skipped, "deleted": deleted}
    logger.info("org=%s  sync complete | %s", org_id, stats)
    return stats
