import asyncio
import hashlib
import logging
import re
from collections import deque
from typing import AsyncGenerator
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
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

_HEADERS = {
    "User-Agent": "Dealership AI/1.0 (RAG indexer)",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _normalize(url: str, base: str = "") -> str:
    full = urljoin(base, url) if base else url
    p = urlparse(full)
    clean = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc, clean, "", p.query, ""))


def _same_domain(url: str, domain: str) -> bool:
    host = urlparse(url).netloc
    return host == domain or host.endswith("." + domain)


def _is_junk(url: str) -> bool:
    return bool(_JUNK_PATH.search(urlparse(url).path))


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


async def _fetch_robots(domain: str, client: httpx.AsyncClient) -> RobotFileParser:
    rp = RobotFileParser()
    try:
        resp = await client.get(f"https://{domain}/robots.txt", timeout=5)
        rp.parse(resp.text.splitlines())
    except Exception:
        pass
    return rp


async def crawl_site(
    start_url: str,
    max_depth: int = 3,
    max_pages: int = 150,
    concurrency: int = 5,
    delay_ms: int = 300,
    timeout_s: int = 15,
) -> AsyncGenerator[dict, None]:
    domain = urlparse(start_url).netloc
    seed = _normalize(start_url)

    queue: deque[tuple[str, int]] = deque([(seed, 0)])
    visited: set[str] = {seed}
    content_hashes: set[str] = set()
    pages_done = 0

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        headers=_HEADERS,
        timeout=httpx.Timeout(timeout_s),
        follow_redirects=True,
        limits=limits,
    ) as client:
        robots = await _fetch_robots(domain, client)
        sem = asyncio.Semaphore(concurrency)

        async def fetch_one(url: str) -> dict | None:
            async with sem:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        return None
                    if "text/html" not in resp.headers.get("content-type", ""):
                        return None
                    html = resp.text
                    ch = hashlib.sha256(html.encode()).hexdigest()
                    if ch in content_hashes:
                        logger.debug("Duplicate content: %s", url)
                        return None
                    content_hashes.add(ch)
                    soup = BeautifulSoup(html, "lxml")
                    title_tag = soup.find("title")
                    title = title_tag.get_text(strip=True) if title_tag else url
                    return {"url": url, "html": html, "title": title}
                except Exception as exc:
                    logger.warning("Failed fetching %s: %s", url, exc)
                    return None
                finally:
                    await asyncio.sleep(delay_ms / 1000)

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
                            and not _is_junk(link)
                            and robots.can_fetch("*", link)
                        ):
                            visited.add(link)
                            queue.append((link, depth + 1))
