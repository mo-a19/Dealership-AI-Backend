import logging
from functools import lru_cache

from pinecone import Pinecone, ServerlessSpec

from app.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_pinecone() -> Pinecone:
    return Pinecone(api_key=get_settings().pinecone_api_key)


async def ensure_index() -> None:
    settings = get_settings()
    pc = get_pinecone()
    existing = {i.name for i in pc.list_indexes()}
    if settings.pinecone_index_name not in existing:
        logger.info("Creating Pinecone index '%s'", settings.pinecone_index_name)
        pc.create_index(
            name=settings.pinecone_index_name,
            dimension=settings.embedding_dimensions,
            metric="cosine",
            spec=ServerlessSpec(cloud=settings.pinecone_cloud, region=settings.pinecone_region),
        )
        logger.info("Index created.")
    else:
        logger.info("Pinecone index '%s' ready.", settings.pinecone_index_name)


def get_index():
    settings = get_settings()
    return get_pinecone().Index(settings.pinecone_index_name)
