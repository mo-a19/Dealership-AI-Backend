import logging
from functools import lru_cache
from typing import List

import tiktoken
from openai import AsyncOpenAI, BadRequestError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import get_settings

logger = logging.getLogger(__name__)

_MAX_EMBED_TOKENS = 8191
_enc = tiktoken.get_encoding("cl100k_base")


def _truncate(text: str) -> str:
    ids = _enc.encode(text)
    if len(ids) <= _MAX_EMBED_TOKENS:
        return text
    logger.warning("Chunk truncated from %d to %d tokens before embedding", len(ids), _MAX_EMBED_TOKENS)
    return _enc.decode(ids[:_MAX_EMBED_TOKENS])


@lru_cache
def get_openai() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=get_settings().openai_api_key)


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception(lambda e: not isinstance(e, BadRequestError)),
    reraise=True,
)
async def _embed_batch(texts: List[str]) -> List[List[float]]:
    settings = get_settings()
    resp = await get_openai().embeddings.create(
        model=settings.embedding_model,
        input=texts,
        dimensions=settings.embedding_dimensions,
    )
    return [e.embedding for e in sorted(resp.data, key=lambda x: x.index)]


async def embed_texts(texts: List[str], batch_size: int = 100) -> List[List[float]]:
    """Embed any number of texts in batches; returns aligned list of vectors."""
    results: List[List[float]] = []
    safe = [_truncate(t) for t in texts]
    for i in range(0, len(safe), batch_size):
        vectors = await _embed_batch(safe[i : i + batch_size])
        results.extend(vectors)
    return results
