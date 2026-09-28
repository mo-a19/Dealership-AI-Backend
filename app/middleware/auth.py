import logging
import time
from dataclasses import dataclass

import httpx
import jwt
from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

logger = logging.getLogger(__name__)

# auto_error=False so we can return 401 (not 403) for missing credentials
_bearer = HTTPBearer(auto_error=False)

_jwks_cache: dict = {"keys": {}, "fetched_at": 0.0}
_JWKS_TTL = 3600


@dataclass
class CurrentUser:
    user_id: str
    org_id: str
    role: str


async def _get_jwks() -> dict:
    now = time.monotonic()
    if _jwks_cache["keys"] and now - _jwks_cache["fetched_at"] < _JWKS_TTL:
        return _jwks_cache["keys"]
    s = get_settings()
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{s.supabase_url}/auth/v1/.well-known/jwks.json", timeout=5)
        r.raise_for_status()
    keys = {k["kid"]: k for k in r.json()["keys"]}
    _jwks_cache.update({"keys": keys, "fetched_at": now})
    return keys


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> CurrentUser:
    if not credentials:
        logger.warning("401 — no Bearer token in request")
        raise HTTPException(
            status_code=401,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    s = get_settings()
    logger.debug("JWT secret configured: %s", bool(s.supabase_jwt_secret))
    try:
        header = jwt.get_unverified_header(token)
        alg = header.get("alg", "")
        logger.debug("JWT alg=%s kid=%s", alg, header.get("kid"))

        decode_kwargs = dict(
            algorithms=[alg],
            audience="authenticated",
            issuer=f"{s.supabase_url}/auth/v1",
        )

        if s.supabase_jwt_secret:
            # HS256 path — verify with the project JWT secret from the Supabase dashboard
            claims = jwt.decode(
                token,
                s.supabase_jwt_secret,
                algorithms=["HS256"],
                audience="authenticated",
                issuer=f"{s.supabase_url}/auth/v1",
            )
        else:
            # JWKS path — RS256 / ES256 asymmetric keys
            jwks = await _get_jwks()
            key_data = jwks.get(header.get("kid"))

            if not key_data:
                # Key not in cache — try a one-time refresh (key rotation)
                _jwks_cache["fetched_at"] = 0.0
                jwks = await _get_jwks()
                key_data = jwks.get(header.get("kid"))

            if not key_data:
                raise ValueError(f"Unknown signing key kid={header.get('kid')}")

            kty = key_data.get("kty", "RSA")
            if kty == "EC":
                public_key = jwt.algorithms.ECAlgorithm.from_jwk(key_data)
            else:
                public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)
            claims = jwt.decode(token, public_key, **decode_kwargs)
    except Exception as exc:
        logger.warning("JWT verification failed: %s", exc)
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # `org_id` and custom `role` are injected at top-level by the custom access
    # token hook; fall back to app_metadata for deployments where the hook is
    # not yet enabled in the Supabase Dashboard.
    app_meta = claims.get("app_metadata") or {}
    org_id = claims.get("org_id") or app_meta.get("org_id")
    logger.debug("JWT sub=%s org_id=%s role=%s", claims.get("sub"), org_id, claims.get("role"))
    if not org_id:
        logger.warning("401 — JWT decoded OK but org_id missing. claims keys: %s", list(claims.keys()))
        raise HTTPException(
            status_code=401,
            detail="Missing org context.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Supabase's built-in `role` claim is always "authenticated"; the custom
    # role lives in app_metadata and is promoted top-level only by the hook.
    raw_role = claims.get("role")
    role = (raw_role if raw_role and raw_role != "authenticated"
            else app_meta.get("role", "viewer"))

    return CurrentUser(
        user_id=claims["sub"],
        org_id=org_id,
        role=role,
    )


def require_role(*roles: str):
    async def _dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in roles:
            raise HTTPException(403, "Insufficient permissions.")
        return user
    return Depends(_dep)
