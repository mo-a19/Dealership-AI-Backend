from functools import lru_cache
from typing import Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # OpenAI
    openai_api_key: str
    embedding_model: str = "text-embedding-3-large"

    embedding_dimensions: int = 1024

    # Pinecone
    pinecone_api_key: str
    pinecone_index_name: str = "rag-scraper"
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"

    # Crawler
    max_crawl_depth: int = 5
    max_pages: int = 300
    crawl_concurrency: int = 5
    request_delay_ms: int = 300
    request_timeout_s: int = 15
    browser_concurrency: int = 3
    browser_networkidle_ms: int = 8000
    js_detect_min_chars: int = 400  # visible-text threshold below which browser is used

    # Chunking
    chunk_target_tokens: int = 450
    chunk_overlap_tokens: int = 50
    min_chunk_tokens: int = 40

    # Retrieval
    top_k_fetch: int = 20
    top_k_return: int = 6
    similarity_threshold: float = 0.25
    retrieval_max_tool_calls: int = 6

    # Supabase
    supabase_url: str
    supabase_anon_key: str
    supabase_service_key: str
    supabase_jwt_secret: Optional[str] = None  # optional: RS256 falls back to JWKS

    # WaSender (WhatsApp)
    wasender_pat: Optional[str] = None  # Personal Access Token — session management
    wasender_base_url: str = "https://www.wasenderapi.com/api"
    public_base_url: Optional[str] = None  # our public https base, for webhook registration

    # Autoflex (CRM / DMS — vehicle inventory knowledge base)
    autoflex_api_url: Optional[str] = None
    autoflex_sync_secret: Optional[str] = None  # shared secret for the cron sync endpoint

    # Email (Resend — https://resend.com)
    resend_api_key: Optional[str] = None
    email_from: str = "noreply@example.com"
    frontend_url: str = "https://app.example.com"

    # PDF ingestion
    pdf_max_size_mb: int = 50
    pdf_vision_enabled: bool = True   # GPT-4o Vision — only invoked when meaningful images exist
    pdf_max_images: int = 20          # cap per PDF to control cost
    pdf_min_image_px: int = 150       # images smaller than N×N are skipped (logos, decorations)

    # App
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
