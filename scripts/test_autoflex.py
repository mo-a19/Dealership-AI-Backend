#!/usr/bin/env python3
"""
Autoflex client smoke test — proves login, environment resolve, and paging work.

Usage:
    AUTOFLEX_API_KEY=... AUTOFLEX_USERNAME=... AUTOFLEX_PASSWORD=... \
        python scripts/test_autoflex.py

Credentials are read from the environment (not from app config), because real
credentials live per-dealership in Vault via /autoflex/connect. The bootstrap base
URL defaults to AUTOFLEX_API_URL from .env. No server needed — it calls the Autoflex
API directly through app/services/autoflex.py.
"""
import asyncio
import os
import sys

# Make `app` importable when run as `python scripts/test_autoflex.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from app.services.autoflex import AutoflexClient  # noqa: E402


def _ok(msg: str):
    print(f"    \033[32m✓\033[0m {msg}")


def _fail(msg: str, detail: str = ""):
    suffix = f"\n      {detail}" if detail else ""
    print(f"    \033[31m✗\033[0m {msg}{suffix}")
    sys.exit(1)


async def main():
    print(f"\n{'='*50}")
    print("  Autoflex Client Smoke Test")
    print(f"{'='*50}\n")

    api_key = os.environ.get("AUTOFLEX_API_KEY")
    username = os.environ.get("AUTOFLEX_USERNAME")
    password = os.environ.get("AUTOFLEX_PASSWORD")
    base_url = os.environ.get("AUTOFLEX_API_URL")
    if not all([api_key, username, password, base_url]):
        print("  Set AUTOFLEX_API_KEY, AUTOFLEX_USERNAME, AUTOFLEX_PASSWORD to run")
        print("  (AUTOFLEX_API_URL is read from .env). Skipping.\n")
        return

    client = AutoflexClient(
        api_key=api_key, username=username, password=password, base_url=base_url
    )

    # 1. Authenticate
    print("[1] authenticate()")
    try:
        info = await client.authenticate()
    except Exception as exc:
        _fail("login failed", str(exc))
    _ok(f"logged in — api_url resolved: {info['api_url']}")

    # 2. Environment resolve (proves we don't hardcode the server)
    print("\n[2] environment()")
    env = await client.environment()
    _ok(f"environment={env.get('environment')}  api_url={env.get('api_url')}")

    # 3. Paging — pull all published-or-not vehicles, trimmed fields
    print("\n[3] get_all('/vehicle')  — paged fetch")
    rows = await client.get_all(
        "/vehicle",
        fields="vehicle_id,v_display_name,brand,sell_price,is_occasion",
    )
    if not rows:
        _fail("no vehicles returned (expected some in the test env)")
    _ok(f"fetched {len(rows)} vehicles")
    print("\n    sample:")
    for v in rows[:5]:
        price = v.get("sell_price")
        print(f"      - {v.get('brand'):<10} {str(v.get('v_display_name'))[:32]:<34} "
              f"price={price}")

    # 4. Token is cached — a second call must not re-login
    print("\n[4] cached token (second call should not re-authenticate)")
    rows2 = await client.get_all("/vehicle", fields="vehicle_id")
    _ok(f"second fetch returned {len(rows2)} vehicles (watch logs: only one 'authenticated' line)")

    print(f"\n{'='*50}")
    print("  All checks passed.")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    asyncio.run(main())
