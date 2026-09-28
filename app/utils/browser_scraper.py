import asyncio
import hashlib
import logging
import re
from collections import deque
from typing import AsyncGenerator
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_JUNK_PATH = re.compile(
    r"(?:"
    r"/(?:login|logout|signin|signup|register|cart|checkout|account|"
    r"password|reset|oauth|auth|cdn-cgi|wp-admin|wp-login|admin)"
    r"|\.(?:json|xml|pdf|zip|png|jpg|jpeg|gif|svg|webp|css|js|ico|"
    r"woff|woff2|ttf|eot|mp4|mp3|wav)(?:\?|$)"
    r")",
    re.I,
)


def _normalize(url: str, base: str = "") -> str:
    full = urljoin(base, url) if base else url
    p = urlparse(full)
    clean = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc, clean, "", p.query, ""))


def _same_domain(url: str, domain: str) -> bool:
    host = urlparse(url).netloc
    return host == domain or host.endswith("." + domain)


def _extract_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        norm = _normalize(href, base_url)
        if norm.startswith("http"):
            links.append(norm)
    return links


async def crawl_site_browser(
    start_url: str,
    max_depth: int = 3,
    max_pages: int = 150,
    concurrency: int = 3,
    delay_ms: int = 500,
    timeout_s: int = 30,
    networkidle_ms: int = 8000,
) -> AsyncGenerator[dict, None]:
    from playwright.async_api import async_playwright

    domain = urlparse(start_url).netloc
    seed = _normalize(start_url)

    queue: deque[tuple[str, int]] = deque([(seed, 0)])
    visited: set[str] = {seed}
    content_hashes: set[str] = set()
    pages_done = 0
    sem = asyncio.Semaphore(concurrency)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (compatible; Dealership AI/1.0)",
            java_script_enabled=True,
            ignore_https_errors=True,
        )

        async def fetch_one(url: str) -> dict | None:
            async with sem:
                page = await ctx.new_page()
                try:
                    await page.goto(url, timeout=timeout_s * 1000, wait_until="domcontentloaded")
                    try:
                        await page.wait_for_load_state("networkidle", timeout=networkidle_ms)
                    except Exception:
                        pass
                    html = await page.content()
                    ch = hashlib.sha256(html.encode()).hexdigest()
                    if ch in content_hashes:
                        logger.debug("Duplicate content skipped: %s", url)
                        return None
                    content_hashes.add(ch)
                    title = await page.title() or url
                    await asyncio.sleep(delay_ms / 1000)
                    return {"url": url, "html": html, "title": title}
                except Exception as exc:
                    logger.warning("Browser fetch failed %s: %s", url, exc)
                    return None
                finally:
                    await page.close()

        try:
            while queue and pages_done < max_pages:
                batch: list[tuple[str, int]] = []
                while queue and len(batch) < concurrency:
                    batch.append(queue.popleft())

                results = await asyncio.gather(*(fetch_one(u) for u, _ in batch))

                for (url, depth), result in zip(batch, results):
                    if result is None:
                        continue
                    pages_done += 1
                    yield result

                    if depth < max_depth and pages_done < max_pages:
                        for link in _extract_links(result["html"], url):
                            if (
                                link not in visited
                                and _same_domain(link, domain)
                                and not _JUNK_PATH.search(urlparse(link).path)
                            ):
                                visited.add(link)
                                queue.append((link, depth + 1))
        finally:
            await browser.close()
