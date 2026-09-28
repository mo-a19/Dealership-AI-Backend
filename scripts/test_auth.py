#!/usr/bin/env python3
"""
Auth + middleware smoke test.

Usage:
    python scripts/test_auth.py [BASE_URL]

Default BASE_URL: http://localhost:8000
Reads .env automatically so SUPABASE_* vars don't need to be exported.
"""
import base64, json, sys, uuid

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE   = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
PREFIX = f"{BASE}/api/v1"

EMAIL    = f"test+{uuid.uuid4().hex[:8]}@example.com"
PASSWORD = "TestPassword123!"
ORG_SLUG = f"test-{uuid.uuid4().hex[:6]}"


# ── helpers ────────────────────────────────────────────────────────────────────

def _decode_claims(token: str) -> dict:
    """Decode JWT payload without verifying signature."""
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def _ok(msg: str):
    print(f"    \033[32m✓\033[0m {msg}")


def _fail(msg: str, detail: str = ""):
    suffix = f"\n      {detail}" if detail else ""
    print(f"    \033[31m✗\033[0m {msg}{suffix}")
    sys.exit(1)


def _check(condition: bool, label: str, detail: str = ""):
    (_ok if condition else _fail)(label, detail)


# ── test cases ─────────────────────────────────────────────────────────────────

print(f"\n{'='*50}")
print(f"  Dealership AI Auth Smoke Test")
print(f"  Base : {PREFIX}")
print(f"  Email: {EMAIL}")
print(f"{'='*50}\n")


# 1. Register ──────────────────────────────────────────────────────────────────
print("[1] Register new user + org")
r = httpx.post(f"{PREFIX}/auth/register", json={
    "email":           EMAIL,
    "password":        PASSWORD,
    "full_name":       "Smoke Test User",
    "dealership_name": f"Smoke Dealer {ORG_SLUG}",
    "org_slug":        ORG_SLUG,
}, timeout=20)
_check(r.status_code == 201, f"POST /auth/register → 201 (got {r.status_code})", r.text)
reg = r.json()
_check("access_token" in reg,                      "response has access_token")
_check(bool(reg.get("user", {}).get("org_id")),    "response user.org_id present")
_check(reg.get("user", {}).get("role") == "admin", "response user.role == admin")
_ok("register passed")


# 2. Login ─────────────────────────────────────────────────────────────────────
print("\n[2] Login")
r = httpx.post(f"{PREFIX}/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=10)
_check(r.status_code == 200, f"POST /auth/login → 200 (got {r.status_code})", r.text)
token = r.json()["access_token"]
_ok("login passed")


# 3. Decode JWT – assert org_id + role in top-level claims ─────────────────────
print("\n[3] JWT claims (hook injection check)")
claims = _decode_claims(token)
_check(bool(claims.get("org_id")), f"org_id in JWT claims  (got: {claims.get('org_id')!r})")
_check(bool(claims.get("role")),   f"role in JWT claims    (got: {claims.get('role')!r})")
_ok(f"org_id={claims['org_id']}   role={claims['role']}")


# 4. Protected route – valid token → 200 or 404 (job not found is fine) ────────
print("\n[4] Protected route – valid token")
auth_headers = {"Authorization": f"Bearer {token}"}
r = httpx.get(f"{PREFIX}/ingestion/scrape/smoke-probe", headers=auth_headers, timeout=10)
_check(
    r.status_code in {200, 404},
    f"valid token → 200|404 (got {r.status_code})",
    r.text,
)
_ok(f"got {r.status_code} (auth accepted)")


# 5. Protected route – no token → 401 ─────────────────────────────────────────
print("\n[5] Protected route – no token")
r = httpx.get(f"{PREFIX}/ingestion/scrape/smoke-probe", timeout=10)
_check(r.status_code == 401, f"no token → 401 (got {r.status_code})", r.text)
_ok("401 without token")


# 6. Protected route – tampered signature → 401 ────────────────────────────────
print("\n[6] Protected route – tampered token")
head, payload, _sig = token.split(".")
tampered = f"{head}.{payload}.dGhpcyBpcyBub3QgdGhlIHJlYWwgc2lnbmF0dXJl"
r = httpx.get(
    f"{PREFIX}/ingestion/scrape/smoke-probe",
    headers={"Authorization": f"Bearer {tampered}"},
    timeout=10,
)
_check(r.status_code == 401, f"tampered token → 401 (got {r.status_code})", r.text)
_ok("401 with tampered signature")


# 7. /health is public ─────────────────────────────────────────────────────────
print("\n[7] Public /health (no token)")
r = httpx.get(f"{PREFIX}/health", timeout=10)
_check(r.status_code == 200, f"/health → 200 (got {r.status_code})", r.text)
_ok("health is public")


# 8. /auth/* routes are public ────────────────────────────────────────────────
print("\n[8] /auth/me with valid token (public route, no middleware dep)")
r = httpx.get(f"{PREFIX}/auth/me", headers=auth_headers, timeout=10)
_check(r.status_code == 200, f"/auth/me → 200 (got {r.status_code})", r.text)
me = r.json()
_check(me.get("org_id") == claims["org_id"], "me.org_id matches JWT claim")
_ok("auth/me passed")


print(f"\n{'='*50}")
print("  All checks passed.")
print(f"{'='*50}\n")
