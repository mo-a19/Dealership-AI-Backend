from datetime import datetime
from enum import Enum
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel


class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class ScrapeRequest(BaseModel):
    url: str


class ScrapeResponse(BaseModel):
    job_id: str
    status: JobStatus
    url: str
    pages_scraped: int = 0
    chunks_indexed: int = 0
    message: str = ""


class QueryRequest(BaseModel):
    question: str
    top_k: Optional[int] = None


class Source(BaseModel):
    url: str
    title: str
    section: str
    score: float


class AppointmentIntent(BaseModel):
    type: str
    notes: str = ""


class QueryResponse(BaseModel):
    answer: str
    sources: List[Source]
    retrieved_chunks: int
    appointment_intents: List[AppointmentIntent] = []
    appointment_type: Optional[str] = None
    appointment_notes: Optional[str] = None


class ChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    top_k: Optional[int] = None


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    sources: List[Source]
    retrieved_chunks: int
    history: List[ChatMessage]


class SessionSummary(BaseModel):
    session_id: str
    title: Optional[str]
    message_count: int
    created_at: str
    updated_at: str


class IngestedSite(BaseModel):
    id: str
    domain: str
    source_url: str
    status: str
    chunk_count: int
    created_at: str


class HealthResponse(BaseModel):
    status: str
    pinecone: bool
    openai: bool
    timestamp: datetime


# --- WhatsApp / WaSender ---

class WhatsAppConnect(BaseModel):
    phone_number: str  # dealership's WhatsApp number, e.g. "923362579505" or "+923362579505"


class WhatsAppConnectResponse(BaseModel):
    session_id: int
    status: str
    qr: Optional[str] = None  # QR string to render client-side; null when already connected


class WhatsAppStatus(BaseModel):
    status: str
    session_id: Optional[int] = None
    phone_number: Optional[str] = None


class ConversationModeUpdate(BaseModel):
    mode: Literal["ai", "human"]
    assigned_to: Optional[UUID] = None  # agent user_id when mode='human'


class AgentReply(BaseModel):
    content: str
    
# --- PDF ingestion ---

class PdfUploadResponse(BaseModel):
    job_id: str
    status: JobStatus
    filename: str
    chunks_indexed: int = 0
    message: str = ""


class PdfDocument(BaseModel):
    id: str
    title: Optional[str]
    filename: str
    status: str
    chunk_count: int
    created_at: str


# --- WhatsApp conversations ---

class MessageItem(BaseModel):
    id: str
    role: str
    content: str
    created_at: str
    media_url: Optional[str] = None
    media_type: Optional[str] = None


class ConversationSummary(BaseModel):
    id: str
    lead_phone: Optional[str] = None
    lead_name: Optional[str] = None
    mode: str
    assigned_to: Optional[str] = None
    created_at: str


class ConversationMessages(BaseModel):
    id: str
    lead_phone: Optional[str] = None
    lead_name: Optional[str] = None
    mode: str
    messages: List[MessageItem]
    created_at: str
