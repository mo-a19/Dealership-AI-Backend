import asyncio
from functools import lru_cache

from supabase import Client, create_client

from app.config import get_settings


@lru_cache
def get_svc() -> Client:
    s = get_settings()
    return create_client(s.supabase_url, s.supabase_service_key)


# --- Supabase Vault helpers (encrypted WaSender secrets) ---

async def vault_write(name: str, secret: str) -> str:
    """Store (or update) an encrypted secret; returns its vault id. Idempotent on name."""
    svc = get_svc()
    res = await asyncio.to_thread(
        lambda: svc.rpc("vault_write", {"p_name": name, "p_secret": secret}).execute()
    )
    return res.data


async def vault_read(secret_id: str) -> str | None:
    """Return the decrypted secret for a vault id, or None if absent."""
    svc = get_svc()
    res = await asyncio.to_thread(
        lambda: svc.rpc("vault_read", {"p_secret_id": secret_id}).execute()
    )
    return res.data
