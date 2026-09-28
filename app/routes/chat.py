import asyncio
import logging
import secrets
import string

from fastapi import APIRouter, Depends, HTTPException

from app.middleware.auth import CurrentUser, get_current_user
from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, SessionSummary
from app.services.appointments import maybe_create_appointment
from app.services.retrieval import retrieve_and_answer
from app.utils.supabase_client import get_svc

router = APIRouter()
logger = logging.getLogger(__name__)

_ALPHABET = string.ascii_letters + string.digits


def _new_session_id() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(12))


# ---------------------------------------------------------------------------
# DB helpers — each does exactly one thing
# ---------------------------------------------------------------------------

async def _load_session(session_id: str, org_id: str) -> list[dict] | None:
    svc = get_svc()
    result = await asyncio.to_thread(
        lambda: svc.table("rag_sessions")
        .select("history")
        .eq("id", session_id)
        .eq("org_id", org_id)
        .execute()
    )
    if not result.data:
        return None
    return result.data[0]["history"]


async def _fetch_ai_settings(org_id: str) -> dict:
    svc = get_svc()
    result = await asyncio.to_thread(
        lambda: svc.table("organizations")
        .select("settings, services")
        .eq("id", org_id)
        .maybe_single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=403, detail="Organization not found. Re-register via /auth/register.")
    s = result.data.get("settings") or {}
    return {
        "system_prompt": s.get("system_prompt") or None,
        "language": s.get("language") or None,
        "services": result.data.get("services") or None,
    }


async def _insert_session(
    session_id: str,
    org_id: str,
    user_id: str,
    history: list[dict],
) -> None:
    """Create a brand-new session row. Title is derived from the first user message."""
    svc = get_svc()
    title = history[0]["content"][:80] if history else None
    await asyncio.to_thread(
        lambda: svc.table("rag_sessions").insert({
            "id": session_id,
            "org_id": org_id,
            "user_id": user_id,
            "title": title,
            "history": history,
        }).execute()
    )


async def _update_session_history(
    session_id: str,
    org_id: str,
    history: list[dict],
) -> None:
    """Persist updated history for an existing session. Title is never touched."""
    svc = get_svc()
    await asyncio.to_thread(
        lambda: svc.table("rag_sessions")
        .update({"history": history})
        .eq("id", session_id)
        .eq("org_id", org_id)
        .execute()
    )


async def _session_exists(session_id: str, org_id: str) -> bool:
    """Lightweight existence check — fetches only the id column."""
    svc = get_svc()
    result = await asyncio.to_thread(
        lambda: svc.table("rag_sessions")
        .select("id")
        .eq("id", session_id)
        .eq("org_id", org_id)
        .execute()
    )
    return bool(result.data)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post(
    "/conversations",
    response_model=ChatResponse,
    summary="Chat with the RAG assistant — every exchange is saved",
)
async def chat(
    req: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
) -> ChatResponse:
    if not req.message.strip():
        raise HTTPException(status_code=422, detail="message must not be empty")

    is_new = req.session_id is None

    if is_new:
        session_id = _new_session_id()
        history = []
        ai_settings = await _fetch_ai_settings(user.org_id)
    else:
        session_id = req.session_id
        history, ai_settings = await asyncio.gather(
            _load_session(session_id, user.org_id),
            _fetch_ai_settings(user.org_id),
        )
        if history is None:
            raise HTTPException(status_code=404, detail="Session not found.")

    result = await retrieve_and_answer(
        question=req.message,
        org_id=user.org_id,
        system_prompt=ai_settings["system_prompt"],
        language=ai_settings["language"],
        services=ai_settings["services"],
        history=history,
    )

    updated_history = [
        *history,
        {"role": "user", "content": req.message},
        {"role": "assistant", "content": result.answer},
    ]

    if result.appointment_intents:
        org = {"id": user.org_id, "services": ai_settings["services"]}
        for intent in result.appointment_intents:
            await maybe_create_appointment(org, None, intent.type, intent.notes or req.message)

    if is_new:
        await _insert_session(session_id, user.org_id, user.user_id, updated_history)
    else:
        await _update_session_history(session_id, user.org_id, updated_history)

    logger.info(
        "Chat | session=%s | org=%s | turn=%d | chunks=%d",
        session_id, user.org_id, len(updated_history) // 2, result.retrieved_chunks,
    )

    return ChatResponse(
        session_id=session_id,
        answer=result.answer,
        sources=result.sources,
        retrieved_chunks=result.retrieved_chunks,
        history=[ChatMessage(role=m["role"], content=m["content"]) for m in updated_history],
    )


@router.get(
    "/conversations",
    response_model=list[SessionSummary],
    summary="List all chat sessions for this dealership",
)
async def list_sessions(
    user: CurrentUser = Depends(get_current_user),
) -> list[SessionSummary]:
    svc = get_svc()
    result = await asyncio.to_thread(
        lambda: svc.table("rag_sessions")
        .select("id, title, history, created_at, updated_at")
        .eq("org_id", user.org_id)
        .order("updated_at", desc=True)
        .execute()
    )
    return [
        SessionSummary(
            session_id=row["id"],
            title=row.get("title"),
            message_count=len(row["history"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        for row in (result.data or [])
    ]


@router.get(
    "/conversations/{session_id}",
    response_model=list[ChatMessage],
    summary="View the full conversation history for a session",
)
async def get_session(
    session_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> list[ChatMessage]:
    history = await _load_session(session_id, user.org_id)
    if history is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return [ChatMessage(role=m["role"], content=m["content"]) for m in history]


@router.delete(
    "/conversations/{session_id}",
    summary="Delete a conversation session",
)
async def delete_session(
    session_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    if not await _session_exists(session_id, user.org_id):
        raise HTTPException(status_code=404, detail="Session not found.")
    svc = get_svc()
    await asyncio.to_thread(
        lambda: svc.table("rag_sessions")
        .delete()
        .eq("id", session_id)
        .eq("org_id", user.org_id)
        .execute()
    )
    logger.info("Session deleted: %s | org=%s", session_id, user.org_id)
    return {"session_id": session_id, "deleted": True}
