import asyncio
import base64
import io
import logging
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath

import fitz
import pdfplumber

from app.config import get_settings
from app.services.chunker import PageChunk, SemanticChunker
from app.utils.embeddings import embed_texts, get_openai
from app.utils.pinecone_client import get_index
from app.utils.supabase_client import get_svc

logger = logging.getLogger(__name__)

_EMBED_BATCH = 80
_UPSERT_BATCH = 100
_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


async def ingest_pdf(job: dict, pdf_bytes: bytes) -> None:
    settings = get_settings()
    chunker = SemanticChunker(
        target=settings.chunk_target_tokens,
        overlap=settings.chunk_overlap_tokens,
        minimum=settings.min_chunk_tokens,
    )
    index = get_index()
    org_id = job["org_id"]
    filename = job["filename"]
    title = PurePosixPath(filename).stem.replace("_", " ").replace("-", " ").title()

    chunks = await _extract_chunks(pdf_bytes, f"pdf://{filename}", title, chunker)
    logger.info("[%s] PDF parsed — %d chunks", job["job_id"], len(chunks))

    buffer = list(chunks)
    indexed = 0
    while len(buffer) >= _EMBED_BATCH:
        batch, buffer = buffer[:_EMBED_BATCH], buffer[_EMBED_BATCH:]
        await _embed_and_upsert(batch, index, org_id, job["kb_doc_id"])
        indexed += len(batch)
    if buffer:
        await _embed_and_upsert(buffer, index, org_id, job["kb_doc_id"])
        indexed += len(buffer)

    job["chunks_indexed"] = indexed


async def delete_pdf_vectors(doc_id: str, org_id: str) -> None:
    """Delete Pinecone vectors + kb_chunks + kb_documents for a PDF."""
    svc = get_svc()
    rows = await asyncio.to_thread(
        lambda: svc.table("kb_chunks")
        .select("pinecone_vector_id")
        .eq("org_id", org_id)
        .eq("doc_id", doc_id)
        .execute()
    )
    vector_ids = [r["pinecone_vector_id"] for r in (rows.data or [])]
    if vector_ids:
        index = get_index()
        for i in range(0, len(vector_ids), _UPSERT_BATCH):
            await asyncio.to_thread(
                index.delete, ids=vector_ids[i: i + _UPSERT_BATCH], namespace=org_id
            )
        logger.info("Deleted %d PDF vectors | doc=%s org=%s", len(vector_ids), doc_id, org_id)

    await asyncio.to_thread(
        lambda: svc.table("kb_documents").delete().eq("id", doc_id).eq("org_id", org_id).execute()
    )


async def _extract_chunks(
    pdf_bytes: bytes, doc_url: str, title: str, chunker: SemanticChunker
) -> list[PageChunk]:
    events: list[tuple] = await asyncio.to_thread(_extract_text_and_tables, pdf_bytes)

    settings = get_settings()
    if settings.pdf_vision_enabled:
        images = await asyncio.to_thread(
            _collect_images, pdf_bytes, settings.pdf_min_image_px, settings.pdf_max_images
        )
        if images:
            logger.info("PDF has %d image(s) — invoking GPT-4o Vision", len(images))
            events.extend(await _describe_images(images))
        else:
            logger.info("No meaningful images — vision model skipped")

    return chunker._build_chunks(events, doc_url, title)


def _extract_text_and_tables(pdf_bytes: bytes) -> list[tuple]:
    events: list[tuple] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            section = f"Page {page_num}"
            try:
                bboxes: list = []
                for table in page.find_tables():
                    bboxes.append(table.bbox)
                    rows = table.extract()
                    if rows:
                        text = _format_table(rows)
                        if text.strip():
                            events.append(("table", text, section))
                for para in _split_paragraphs(_text_outside_tables(page, bboxes)):
                    events.append(("paragraph", para, section))
            except Exception as exc:
                logger.warning("Page %d extraction failed — falling back to plain text: %s", page_num, exc)
                for para in _split_paragraphs(page.extract_text() or ""):
                    events.append(("paragraph", para, section))
    return events


def _text_outside_tables(page, bboxes: list) -> str:
    if not bboxes:
        return page.extract_text(x_tolerance=3, y_tolerance=3) or ""

    def keep(obj):
        if obj.get("object_type") != "char":
            return True
        return not any(
            b[0] - 2 <= obj.get("x0", 0) and obj.get("x1", 0) <= b[2] + 2 and
            b[1] - 2 <= obj.get("top", 0) and obj.get("bottom", 0) <= b[3] + 2
            for b in bboxes
        )

    try:
        return page.filter(keep).extract_text(x_tolerance=3, y_tolerance=3) or ""
    except Exception:
        return page.extract_text(x_tolerance=3, y_tolerance=3) or ""


def _format_table(rows: list) -> str:
    lines: list[str] = []
    for i, row in enumerate(rows):
        cells = [str(c or "").strip() for c in row]
        if not any(cells):
            continue
        lines.append(" | ".join(cells))
        if i == 0:
            lines.append("-" * max(len(lines[0]), 3))
    return "\n".join(lines)


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n{2,}", text) if len(p.strip()) > 20]


def _collect_images(pdf_bytes: bytes, min_px: int, max_images: int) -> list[tuple]:
    results: list[tuple] = []
    min_area = min_px * min_px
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page_idx in range(len(doc)):
            if len(results) >= max_images:
                break
            for img_info in doc[page_idx].get_images():
                if len(results) >= max_images:
                    break
                base = doc.extract_image(img_info[0])
                img_bytes = base.get("image", b"")
                if base.get("width", 0) * base.get("height", 0) < min_area or len(img_bytes) < 4096:
                    continue
                results.append((page_idx + 1, img_bytes, base.get("ext", "png")))
    finally:
        doc.close()
    return results


async def _describe_images(images: list[tuple]) -> list[tuple]:
    client = get_openai()
    tasks = [_describe_one(client, img_bytes, ext) for _, img_bytes, ext in images]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    events: list[tuple] = []
    for (page_num, _, _), result in zip(images, results):
        if isinstance(result, Exception):
            logger.warning("Vision failed on page %d: %s", page_num, result)
        elif result:
            events.append(("paragraph", f"[Visual on page {page_num}]: {result}", f"Page {page_num}"))
    return events


async def _describe_one(client, img_bytes: bytes, ext: str) -> str:
    mime = _MIME.get(ext.lower(), "image/jpeg")
    resp = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "This image is from a car dealership document. "
                        "Extract all visible information: text, numbers, prices, specs, "
                        "table data, chart values. Be specific. "
                        "If purely decorative or a car photo with no text, reply 'decorative' only."
                    ),
                },
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64,{base64.b64encode(img_bytes).decode()}",
                    "detail": "high",
                }},
            ],
        }],
        max_tokens=600,
        timeout=30,
    )
    text = (resp.choices[0].message.content or "").strip()
    return "" if text.lower().startswith("decorative") else text


async def _embed_and_upsert(
    chunks: list[PageChunk], index, org_id: str, kb_doc_id: str
) -> None:
    vectors = await embed_texts([c.text for c in chunks])
    indexed_at = datetime.now(timezone.utc).isoformat()

    records, chunk_rows = [], []
    for chunk, vec in zip(chunks, vectors):
        records.append({
            "id": chunk.content_hash,
            "values": vec,
            "metadata": {
                "url": chunk.url,
                "doc_id": kb_doc_id,
                "org_id": org_id,
                "title": chunk.title,
                "section_path": chunk.section_path,
                "chunk_type": chunk.chunk_type,
                "chunk_index": chunk.chunk_index,
                "token_count": chunk.token_count,
                "text": chunk.text[:4000],
                "source_type": "pdf",
                "indexed_at": indexed_at,
            },
        })
        chunk_rows.append({
            "org_id": org_id,
            "doc_id": kb_doc_id,
            "pinecone_vector_id": chunk.content_hash,
            "chunk_index": chunk.chunk_index,
            "content_preview": chunk.text[:200],
            "token_count": chunk.token_count,
        })

    for i in range(0, len(records), _UPSERT_BATCH):
        await asyncio.to_thread(index.upsert, vectors=records[i: i + _UPSERT_BATCH], namespace=org_id)

    seen: set[str] = set()
    unique_rows = []
    for r in chunk_rows:
        if r["pinecone_vector_id"] not in seen:
            seen.add(r["pinecone_vector_id"])
            unique_rows.append(r)

    svc = get_svc()
    await asyncio.to_thread(
        lambda: svc.table("kb_chunks").upsert(unique_rows, on_conflict="pinecone_vector_id").execute()
    )
