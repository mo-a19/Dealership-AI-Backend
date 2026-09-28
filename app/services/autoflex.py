"""Async client for the Autoflex 10 REST API (vehicle-inventory knowledge source).

Auth model (two credentials):
  - An integration **API key** — sent as the `apikey` header on data calls, and as
    the `api_key` query param on `/authenticate`.
  - An **API user** (username + password) — exchanged at `/authenticate` for a
    short-lived `token`. That same response also returns the authoritative
    `api_url`, which we use for every later call, so moving test → production needs
    no code change (never hardcode the `.work` host beyond the bootstrap URL).

Token handling is automatic: we re-authenticate proactively before the stated
expiry, and reactively on any 401 / "Not authorized" reply. The `/refreshtoken`
endpoint is intentionally not used — it needs params the token flow doesn't expose,
and a fresh login is a single cheap GET.

Built as a small class (not module functions like `wasender.py`) because each
dealership has its own credentials and token. Per-org clients are built from the
dealership's Vault credentials via `build_org_client()`.
"""
import asyncio
import logging
import time
from datetime import datetime

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0)
# Force IPv4 — uvloop does not fall back from IPv6 to IPv4 (same as wasender.py).
_TRANSPORT = httpx.AsyncHTTPTransport(local_address="0.0.0.0")

_TOKEN_SKEW = 60        # re-auth this many seconds before the stated expiry
_TOKEN_MAX_AGE = 1800   # hard cap: re-auth at least every 30 min (token_valid_until has no tz)


class AutoflexError(RuntimeError):
    """Raised when Autoflex returns an error payload or an unexpected response."""


class AutoflexClient:
    """Stateful, token-managing client for one Autoflex account."""

    def __init__(self, *, api_key: str | None, username: str | None,
                 password: str | None, base_url: str | None):
        if not all([api_key, username, password, base_url]):
            raise AutoflexError(
                "AutoflexClient needs api_key, username, password and base_url."
            )
        self._api_key = api_key
        self._username = username
        self._password = password
        self._bootstrap_url = base_url.rstrip("/")
        self._api_url = self._bootstrap_url  # replaced by the api_url returned at login
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    # --- authentication --------------------------------------------------

    async def _authenticate(self) -> None:
        params = {
            "api_key": self._api_key,
            "username": self._username,
            "password": self._password,
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=_TRANSPORT) as client:
            r = await client.get(f"{self._bootstrap_url}/authenticate", params=params)
        data = _json_or_error(r, "authenticate")
        token = data.get("token")
        if not token:
            raise AutoflexError("authenticate returned no token")
        self._token = token
        self._api_url = (data.get("api_url") or self._api_url).rstrip("/")
        self._expires_at = _expiry_epoch(data.get("token_valid_until"))
        logger.info(
            "Autoflex authenticated user=%s env=%s api_url=%s",
            self._username, data.get("environment"), self._api_url,
        )

    async def _ensure_token(self) -> None:
        async with self._lock:
            if self._token and time.time() < self._expires_at:
                return
            await self._authenticate()

    def _headers(self) -> dict:
        return {"token": self._token, "apikey": self._api_key}

    async def _request(self, method: str, path: str, *, params: dict | None = None) -> dict:
        """One request, with a single transparent re-auth + retry on 401."""
        await self._ensure_token()
        url = f"{self._api_url}{path}"
        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=_TRANSPORT) as client:
            r = await client.request(method, url, headers=self._headers(), params=params)
            if r.status_code == 401 or _is_auth_error(r):
                async with self._lock:
                    await self._authenticate()
                r = await client.request(method, url, headers=self._headers(), params=params)
        return _json_or_error(r, path)

    # --- public API ------------------------------------------------------

    async def authenticate(self) -> dict:
        """Force a login now and return basic session info. Used to validate
        credentials when a dealership connects their Autoflex account."""
        async with self._lock:
            await self._authenticate()
        return {"api_url": self._api_url, "token_expires_at": self._expires_at}

    async def environment(self) -> dict:
        """Resolve environment + authoritative api_url for this user (no login)."""
        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=_TRANSPORT) as client:
            r = await client.get(
                f"{self._bootstrap_url}/util/environment",
                headers={"apikey": self._api_key},
                params={"username": self._username},
            )
        return _json_or_error(r, "util/environment")

    async def get(self, path: str, *, params: dict | None = None) -> dict:
        return await self._request("GET", path, params=params)

    async def get_all(
        self,
        path: str,
        *,
        fields: str | list[str] | None = None,
        filter: str | None = None,
        search: str | None = None,
        sort: str | None = None,
    ) -> list[dict]:
        """Fetch every page of a list endpoint and return the combined rows.

        Autoflex returns `{"data": [...], "nextpage": bool}` and pages via a 1-based
        `page` query param (~100 rows/page). `fields` trims the response to the
        columns we actually need.
        """
        base: dict = {}
        if fields:
            base["fields"] = fields if isinstance(fields, str) else ",".join(fields)
        if filter:
            base["filter"] = filter
        if search:
            base["search"] = search
        if sort:
            base["sort"] = sort

        rows: list[dict] = []
        page = 1
        while True:
            payload = await self._request("GET", path, params={**base, "page": page})
            data = payload.get("data") if isinstance(payload, dict) else payload
            if isinstance(data, list):
                rows.extend(data)
            if not (isinstance(payload, dict) and payload.get("nextpage")):
                break
            page += 1
        logger.info("Autoflex GET %s → %d rows across %d page(s)", path, len(rows), page)
        return rows


# --- helpers -------------------------------------------------------------

def _json_or_error(r: httpx.Response, ctx: str) -> dict:
    try:
        payload = r.json() if r.content else {}
    except ValueError:
        raise AutoflexError(f"{ctx}: non-JSON response (HTTP {r.status_code})")
    if isinstance(payload, dict) and payload.get("errorCode"):
        raise AutoflexError(
            f"{ctx}: {payload.get('errorMessage')} (code {payload.get('errorCode')})"
        )
    if r.status_code >= 400:
        raise AutoflexError(f"{ctx}: HTTP {r.status_code}")
    return payload


def _is_auth_error(r: httpx.Response) -> bool:
    try:
        p = r.json()
    except ValueError:
        return False
    return isinstance(p, dict) and (
        p.get("errorCode") == 401 or p.get("errorMessage") == "Not authorized"
    )


def _expiry_epoch(token_valid_until: str | None) -> float:
    """Turn Autoflex's `YYYYMMDDTHHMMSS` (no timezone) into an epoch deadline.

    Because the timezone is unknown we never trust the raw value blindly: we cap the
    lifetime at `_TOKEN_MAX_AGE` and lean on the 401-retry as the real safety net.
    """
    now = time.time()
    fallback = now + _TOKEN_MAX_AGE
    if not token_valid_until:
        return fallback
    try:
        stated = datetime.strptime(token_valid_until, "%Y%m%dT%H%M%S").timestamp() - _TOKEN_SKEW
    except ValueError:
        return fallback
    if stated <= now:
        # tz offset made it look already-expired — fall back to the cap + retry.
        return fallback
    return min(stated, fallback)


async def build_org_client(org_id: str) -> AutoflexClient:
    """Construct a client from a dealership's stored (Vault) Autoflex credentials.

    Used by the sync pipeline (#19/#20). Local imports avoid an import cycle, the
    same way wasender.reconnect_existing_sessions does.
    """
    from app.utils.supabase_client import get_svc, vault_read  # local import avoids cycle

    svc = get_svc()
    org = await asyncio.to_thread(
        lambda: svc.table("organizations")
        .select(
            "autoflex_api_key_id, autoflex_username_id, autoflex_password_id, "
            "autoflex_api_url, autoflex_enabled"
        )
        .eq("id", org_id)
        .maybe_single()
        .execute()
    )
    d = org.data or {}
    if not d.get("autoflex_enabled"):
        raise AutoflexError("Autoflex is not connected for this organization.")

    api_key = await vault_read(d["autoflex_api_key_id"]) if d.get("autoflex_api_key_id") else None
    username = await vault_read(d["autoflex_username_id"]) if d.get("autoflex_username_id") else None
    password = await vault_read(d["autoflex_password_id"]) if d.get("autoflex_password_id") else None
    base_url = d.get("autoflex_api_url") or get_settings().autoflex_api_url
    return AutoflexClient(
        api_key=api_key, username=username, password=password, base_url=base_url
    )
