import asyncio
import json
import logging
import math
from typing import List, Optional

from app.config import get_settings
from app.models.schemas import AppointmentIntent, QueryResponse, Source
from app.utils.embeddings import embed_texts, get_openai
from app.utils.pinecone_client import get_index

logger = logging.getLogger(__name__)

NOT_FOUND_RESPONSE = (
    "I don't have that detail on hand right now. "
    "For the most accurate information, I'd recommend reaching out to us directly — "
    "we'd be happy to help!"
)

_DEFAULT_SYSTEM_PROMPT = (
    "You are a warm, knowledgeable sales assistant at this dealership. "
    "Your job is to help customers find the right vehicle and answer their questions "
    "with genuine care and expertise.\n\n"
    "Rules you must always follow:\n"
    "- Always search the dealership knowledge base before answering. Never answer from memory or training data.\n"
    "- Answer only from what the search returns. If something isn't in the results, be honest: "
    "tell the customer you don't have that detail and suggest they contact the dealership directly.\n"
    "- Be conversational and natural — like a helpful, friendly colleague, not a chatbot or a manual.\n"
    "- Keep responses short and clear. 2–4 sentences is ideal unless a full breakdown is clearly needed.\n"
    "- Never fabricate prices, specs, availability, or financing terms.\n"
    "- When a customer shows interest in a vehicle, naturally encourage the next step: "
    "a test drive, visiting the showroom, or speaking with an advisor."
)

# _TOOLS_GATHER — first call only: model must understand intent and fetch info before acting
# _TOOLS — subsequent calls: model can also signal booking intent via create_appointment
_TOOLS_GATHER = [
    {
        "type": "function",
        "function": {
            "name": "search_website",
            "description": (
                "Search the dealership knowledge base — vehicle stock, prices, specs, colors, variants, "
                "availability, and website content. "
                "Use for: vehicle information, prices, inventory, and specs. "
                "Do NOT use for: service bookings, appointments, oil changes, test drives, maintenance, "
                "trade-ins, or any question about what services are offered — use get_services for those. "
                "Write descriptive queries, not single generic words. "
                "For specific vehicles use the EXACT model name the user mentioned."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Exact search query using the precise model name the user mentioned.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_services",
            "description": (
                "Return the complete list of services this dealership offers, including booking links and instructions. "
                "Use for ANY of these: oil change, test drive, maintenance, APK/MOT inspection, trade-in, "
                "financing, damage repair, parts, or any question about services, bookings, or appointments. "
                "Always call this before answering service-related questions — never guess from memory."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

_TOOLS = _TOOLS_GATHER + [
    {
        "type": "function",
        "function": {
            "name": "create_appointment",
            "description": (
                "Signal that the customer explicitly wants to book or schedule a specific service. "
                "Only call this AFTER get_services has returned — use the exact 'type' string from that list. "
                "Call when the customer directly requests booking — e.g. 'I need maintenance', "
                "'book me a test drive', 'I want an oil change', 'ik wil een onderhoudsbeurt'. "
                "Do NOT call this when the customer is just asking about services, prices, or availability."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "description": "Exact service type from get_services (e.g. 'maintenance', 'oil_change', 'test_drive')",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Brief summary of what the customer said",
                    },
                },
                "required": ["type"],
            },
        },
    },
]

_REWRITE_SYSTEM = (
    "Rewrite the follow-up question as a standalone search query using conversation context. "
    "CRITICAL: Keep model names exactly as the user said them — never paraphrase or substitute model names. "
    "Return ONLY the rewritten question, nothing else."
)

_COMPLEX_KEYWORDS = {
    "compare", "vs", "versus", "difference", "better", "recommend", "which",
    "loan", "finance", "installment", "monthly", "calculate", "budget",
    "all models", "full list", "lineup", "range",
}


def _pick_model(question: str, history: list) -> str:
    q = question.lower()
    if any(kw in q for kw in _COMPLEX_KEYWORDS) or len(history) > 6:
        return "gpt-4o"
    return "gpt-4o-mini"


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


async def _rewrite_question(question: str, history: List[dict]) -> str:
    """Rewrite a follow-up question into a standalone query using conversation history."""
    client = get_openai()

    history_text = "\n".join(
        f"{m['role'].capitalize()}: {m['content']}" for m in history[-6:]
    )
    prompt = f"Conversation so far:\n{history_text}\n\nFollow-up: {question}"

    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _REWRITE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        max_tokens=128,
    )
    rewritten = resp.choices[0].message.content or question
    logger.info("Query rewrite: %r → %r", question[:60], rewritten[:60])
    return rewritten


async def _search_chunks(query: str, org_id: str, settings) -> list[dict]:
    """Embed query, search Pinecone namespace for this dealership, return top chunks."""
    index = get_index()
    q_vec = (await embed_texts([query]))[0]

    raw = await asyncio.to_thread(
        index.query,
        vector=q_vec,
        top_k=settings.top_k_fetch,
        include_metadata=True,
        include_values=True,  # needed for cosine-similarity MMR
        namespace=org_id,
    )

    chunks = []
    seen_urls: dict[str, float] = {}
    for m in raw.matches:
        if m.score < settings.similarity_threshold:
            continue
        url = m.metadata.get("url", "")
        if url in seen_urls and seen_urls[url] >= m.score:
            continue
        seen_urls[url] = m.score
        chunks.append({
            "url": url,
            "title": m.metadata.get("title", ""),
            "section_path": m.metadata.get("section_path", ""),
            "text": m.metadata.get("text", ""),
            "score": m.score,
            "embedding": list(m.values) if m.values else [],
        })

    logger.info(
        "search_website(%r) org_id=%s → %d chunks (threshold=%.2f)",
        query, org_id, len(chunks), settings.similarity_threshold,
    )
    return chunks


async def retrieve_and_answer(
    question: str,
    org_id: str,
    system_prompt: str | None = None,
    top_k: int | None = None,
    history: Optional[List[dict]] = None,
    language: str | None = None,
    customer_name: str | None = None,
    max_reply_tokens: int = 1500,
    services: Optional[List[dict]] = None,
) -> QueryResponse:
    settings = get_settings()
    k_return = top_k if top_k and top_k > 0 else settings.top_k_return
    client = get_openai()
    history = history or []

    model = _pick_model(question, history)
    logger.info("Retrieval start | question=%r | org_id=%s | model=%s | lang=%s", question[:80], org_id, model, language)

    active_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT
    if customer_name:
        active_prompt += f"\n\nThe customer's name is {customer_name}. Use their name naturally — once or twice, not every message."
    if language:
        active_prompt += (
            f"\n\nCRITICAL: You MUST always respond in {language}. "
            "Never respond in any other language regardless of what language the user writes in."
        )
    messages: list[dict] = [{"role": "system", "content": active_prompt}]
    for h in history[-8:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": question})

    all_chunks: dict[str, dict] = {}
    answer = NOT_FOUND_RESPONSE
    detected_intents: list[AppointmentIntent] = []
    tool_calls_made = 0
    max_calls = settings.retrieval_max_tool_calls
    services_fetched = False  # gate: create_appointment blocked until get_services is called

    for i in range(max_calls + 1):
        # i=0: gather only — model must call get_services to see the org's actual type strings
        #       before create_appointment is available. Prevents type guessing.
        # i≥1: full tools — model can now call create_appointment with the exact type it saw.
        tools_this_call = _TOOLS_GATHER if i == 0 else _TOOLS
        # Force a tool call only on the very first turn of a fresh conversation.
        # Follow-up messages (customer answering collected fields) must be free to
        # reply directly without being pushed into a search_website call.
        force_tool = i == 0 and not history
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools_this_call,
            tool_choice="required" if force_tool else "auto",
            temperature=0.2,
            max_tokens=max_reply_tokens,
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            answer = msg.content or NOT_FOUND_RESPONSE
            break

        if tool_calls_made >= max_calls:
            messages.append({"role": "assistant", "content": None, "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": "Search limit reached."})
            final = await client.chat.completions.create(
                model=model, messages=messages, temperature=0, max_tokens=1500,
            )
            answer = final.choices[0].message.content or NOT_FOUND_RESPONSE
            break

        search_calls: list[tuple[str, str]] = []
        tc_results: dict[str, str] = {}

        for tc in msg.tool_calls:
            if tc.function.name == "create_appointment" and not services_fetched:
                tc_results[tc.id] = json.dumps({
                    "error": "You must call get_services first to read the required fields for this service before creating an appointment."
                })

            elif tc.function.name == "get_services":
                services_fetched = True
                formatted = []
                for s in (services or []):
                    if not s.get("enabled", True):
                        continue
                    entry: dict = {"service": s.get("label", s.get("type")), "type": s.get("type")}
                    if s.get("estimated_duration"):
                        entry["estimated_duration"] = s["estimated_duration"]
                    if s.get("auto_schedule") and s.get("booking_url"):
                        entry["action"] = "share_booking_link"
                        entry["booking_url"] = s["booking_url"]
                    else:
                        entry["action"] = "collect_details_and_register"
                        fields = s.get("required_fields") or []
                        faq = s.get("faq") or []
                        if fields:
                            required = [f for f in fields if f.get("required", True)]
                            optional = [f for f in fields if not f.get("required", True)]
                            entry["fields"] = fields
                            next_required = required[0]["label"] if required else None
                            entry["instruction"] = (
                                "Collect customer information one field at a time. "
                                "IMPORTANT: Ask for ONE field only per message — never list all fields at once. "
                                "Check conversation history to find the first field not yet answered, then ask ONLY for that one. "
                                "For type='text': ask the customer to type their answer. "
                                "For type='media': ask the customer to send a photo or video. "
                                "NEVER call search_website to collect the customer's personal details — just ask them directly. "
                                "Only call search_website or FAQ if the customer asks an unrelated question mid-flow, then return to collecting. "
                                f"Fields to collect in order: {[f['label'] for f in fields]}. "
                                f"Required (block appointment until done): {[f['label'] for f in required]}. "
                                + (f"Optional (collect if possible): {[f['label'] for f in optional]}. " if optional else "")
                                + (f"Start by asking for: {next_required}. " if next_required else "")
                                + "Once all required fields are collected, call create_appointment with a summary in notes."
                            )
                        else:
                            entry["instruction"] = (
                                "1. Call create_appointment with this service type RIGHT NOW to register intent. "
                                "2. Ask for preferred date/time and any relevant details. "
                                "3. Confirm the team will contact them to finalise. "
                                "Do NOT skip step 1."
                            )
                        if faq:
                            entry["faq"] = faq
                    formatted.append(entry)
                tc_results[tc.id] = json.dumps(formatted)

            elif tc.function.name == "create_appointment":
                try:
                    args = json.loads(tc.function.arguments)
                    appt_type = args.get("type")
                    if appt_type:
                        detected_intents.append(AppointmentIntent(
                            type=appt_type,
                            notes=args.get("notes", ""),
                        ))
                except (json.JSONDecodeError, KeyError):
                    pass
                tc_results[tc.id] = json.dumps({"status": "noted"})

            else:
                try:
                    args = json.loads(tc.function.arguments)
                    search_calls.append((tc.id, args.get("query", "")))
                except (json.JSONDecodeError, KeyError):
                    search_calls.append((tc.id, ""))

        if search_calls:
            search_results = await asyncio.gather(
                *(_search_chunks(q, org_id, settings) for _, q in search_calls)
            )
            for (tc_id, _), chunks in zip(search_calls, search_results):
                for c in chunks:
                    url = c["url"]
                    if url not in all_chunks or c["score"] > all_chunks[url]["score"]:
                        all_chunks[url] = c
                tc_results[tc_id] = (
                    json.dumps([{"url": c["url"], "section": c["section_path"], "content": c["text"]} for c in chunks])
                    if chunks else json.dumps({"message": "No relevant content found for this query."})
                )

        tool_calls_made += len(msg.tool_calls)
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        })
        for tc in msg.tool_calls:
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": tc_results[tc.id]})

    candidates = sorted(all_chunks.values(), key=lambda x: x["score"], reverse=True)
    selected = _mmr(candidates, k=k_return, lam=0.65)

    sources = [
        Source(url=c["url"], title=c["title"], section=c["section_path"], score=round(c["score"], 4))
        for c in selected
    ]
    logger.info(
        "Answered with %d total chunks | org_id=%s | intents=%s",
        len(all_chunks), org_id, [i.type for i in detected_intents],
    )
    first = detected_intents[0] if detected_intents else None
    return QueryResponse(
        answer=answer,
        sources=sources,
        retrieved_chunks=len(all_chunks),
        appointment_intents=detected_intents,
        appointment_type=first.type if first else None,
        appointment_notes=(first.notes if first else None) or question[:300],
    )


def _mmr(candidates: List[dict], k: int, lam: float = 0.65) -> List[dict]:
    if len(candidates) <= k:
        return candidates
    selected = [candidates[0]]
    remaining = candidates[1:]
    while len(selected) < k and remaining:
        scored = []
        for cand in remaining:
            max_sim = max(_cosine(cand["embedding"], s["embedding"]) for s in selected)
            scored.append((lam * cand["score"] - (1 - lam) * max_sim, cand))
        scored.sort(key=lambda x: x[0], reverse=True)
        selected.append(scored[0][1])
        remaining = [s[1] for s in scored[1:]]
    return selected
