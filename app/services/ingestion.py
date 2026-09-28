import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings
from app.models.schemas import JobStatus
from app.services.chunker import PageChunk, SemanticChunker
from app.utils.browser_scraper import crawl_site_browser
from app.utils.embeddings import embed_texts
from app.utils.pinecone_client import get_index
from app.utils.scraper import crawl_site
from app.utils.supabase_client import get_svc

logger = logging.getLogger(__name__)

_EMBED_BATCH = 80
_UPSERT_BATCH = 100

_PROBE_HEADERS = {
    "User-Agent": "Dealership AI/1.0 (RAG indexer)",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}


async def _needs_browser(url: str, min_chars: int) -> bool:
    try:
        async with httpx.AsyncClient(
            headers=_PROBE_HEADERS, timeout=10, follow_redirects=True
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return True
            soup = BeautifulSoup(resp.text, "lxml")
            for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            is_thin = len(text) < min_chars
            logger.info(
                "JS probe: %d visible chars → %s",
                len(text),
                "browser" if is_thin else "httpx",
            )
            return is_thin
    except Exception as exc:
        logger.warning("JS probe failed (%s) — defaulting to browser", exc)
        return True


async def ingest_site(job: dict) -> None:
    settings = get_settings()
    chunker = SemanticChunker(
        target=settings.chunk_target_tokens,
        overlap=settings.chunk_overlap_tokens,
        minimum=settings.min_chunk_tokens,
    )
    index = get_index()
    org_id = job["org_id"]
    domain = urlparse(job["url"]).netloc

    use_browser = await _needs_browser(job["url"], settings.js_detect_min_chars)
    logger.info("[%s] Starting ingestion use_browser=%s", job["job_id"], use_browser)

    buffer: list[PageChunk] = []

    if use_browser:
        crawler = crawl_site_browser(
            start_url=job["url"],
            max_depth=settings.max_crawl_depth,
            max_pages=settings.max_pages,
            concurrency=settings.browser_concurrency,
            delay_ms=settings.request_delay_ms,
            timeout_s=settings.request_timeout_s,
            networkidle_ms=settings.browser_networkidle_ms,
        )
    else:
        crawler = crawl_site(
            start_url=job["url"],
            max_depth=settings.max_crawl_depth,
            max_pages=settings.max_pages,
            concurrency=settings.crawl_concurrency,
            delay_ms=settings.request_delay_ms,
            timeout_s=settings.request_timeout_s,
        )

    async for page in crawler:
        chunks = chunker.chunk_page(page["html"], page["url"], page["title"])
        buffer.extend(chunks)
        job["pages_scraped"] += 1
        logger.info(
            "[%s] Page %d: %s  (+%d chunks)",
            job["job_id"], job["pages_scraped"], page["url"], len(chunks),
        )

        while len(buffer) >= _EMBED_BATCH:
            batch, buffer = buffer[:_EMBED_BATCH], buffer[_EMBED_BATCH:]
            await _embed_and_upsert(batch, index, org_id, domain, job["kb_doc_id"])
            job["chunks_indexed"] += len(batch)

    if buffer:
        await _embed_and_upsert(buffer, index, org_id, domain, job["kb_doc_id"])
        job["chunks_indexed"] += len(buffer)

    logger.info(
        "[%s] Ingestion done — %d pages, %d chunks",
        job["job_id"], job["pages_scraped"], job["chunks_indexed"],
    )


async def _embed_and_upsert(
    chunks: list[PageChunk], index, org_id: str, domain: str, kb_doc_id: str
) -> None:
    texts = [c.text for c in chunks]
    vectors = await embed_texts(texts)
    scraped_at = datetime.now(timezone.utc).isoformat()

    records = []
    kb_chunk_rows = []
    for chunk, vec in zip(chunks, vectors):
        vector_id = chunk.content_hash
        records.append({
            "id": vector_id,
            "values": vec,
            "metadata": {
                "url": chunk.url,
                "domain": domain,
                "org_id": org_id,
                "title": chunk.title,
                "section_path": chunk.section_path,
                "chunk_type": chunk.chunk_type,
                "chunk_index": chunk.chunk_index,
                "token_count": chunk.token_count,
                "text": chunk.text[:4000],
                "scraped_at": scraped_at,
            },
        })
        kb_chunk_rows.append({
            "org_id": org_id,
            "doc_id": kb_doc_id,
            "pinecone_vector_id": vector_id,
            "chunk_index": chunk.chunk_index,
            "content_preview": chunk.text[:200],
            "token_count": chunk.token_count,
        })

    for i in range(0, len(records), _UPSERT_BATCH):
        batch = records[i : i + _UPSERT_BATCH]
        await asyncio.to_thread(index.upsert, vectors=batch, namespace=org_id)

    # Deduplicate by pinecone_vector_id — identical chunks produce the same hash
    seen: set[str] = set()
    unique_rows = []
    for row in kb_chunk_rows:
        if row["pinecone_vector_id"] not in seen:
            seen.add(row["pinecone_vector_id"])
            unique_rows.append(row)

    svc = get_svc()
    await asyncio.to_thread(
        lambda: svc.table("kb_chunks")
        .upsert(unique_rows, on_conflict="pinecone_vector_id")
        .execute()
    )
