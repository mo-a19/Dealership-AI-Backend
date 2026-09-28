import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


async def send_email(to: str, subject: str, html: str) -> bool:
    s = get_settings()
    if not s.resend_api_key:
        logger.warning("RESEND_API_KEY not set — email to %s skipped", to)
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {s.resend_api_key}"},
                json={"from": s.email_from, "to": to, "subject": subject, "html": html},
            )
            r.raise_for_status()
            return True
    except Exception:
        logger.exception("Failed to send email to %s", to)
        return False
