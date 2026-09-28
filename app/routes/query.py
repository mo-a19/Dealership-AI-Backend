import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from app.middleware.auth import CurrentUser, get_current_user
from app.models.schemas import QueryRequest, QueryResponse
from app.services.retrieval import retrieve_and_answer
from app.utils.supabase_client import get_svc

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/search", response_model=QueryResponse, summary="Ask a question about an indexed website")
async def query(
    req: QueryRequest,
    user: CurrentUser = Depends(get_current_user),
) -> QueryResponse:
    if not req.question.strip():
        raise HTTPException(status_code=422, detail="question must not be empty")

    svc = get_svc()
    org_row = await asyncio.to_thread(
        lambda: svc.table("organizations").select("settings").eq("id", user.org_id).maybe_single().execute()
    )
    if not org_row.data:
        raise HTTPException(status_code=403, detail="Organization not found. Re-register via /auth/register.")
    s = org_row.data.get("settings") or {}
    result = await retrieve_and_answer(
        question=req.question,
        org_id=user.org_id,
        system_prompt=s.get("system_prompt") or None,
        language=s.get("language") or None,
        top_k=req.top_k,
    )
    logger.info("Query answered with %d chunks | org_id=%s", result.retrieved_chunks, user.org_id)
    return result
