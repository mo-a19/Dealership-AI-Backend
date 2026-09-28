from datetime import datetime, timezone

from fastapi import APIRouter

from app.models.schemas import HealthResponse

router = APIRouter(tags=["Platform"])


@router.get("/health", response_model=HealthResponse, summary="Health check")
async def health() -> HealthResponse:
    from app.utils.embeddings import get_openai
    from app.utils.pinecone_client import get_pinecone

    pc_ok = oai_ok = False
    try:
        get_pinecone().list_indexes()
        pc_ok = True
    except Exception:
        pass
    try:
        get_openai()
        oai_ok = True
    except Exception:
        pass

    return HealthResponse(
        status="ok" if pc_ok and oai_ok else "degraded",
        pinecone=pc_ok,
        openai=oai_ok,
        timestamp=datetime.now(timezone.utc),
    )
