import asyncio
import logging
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from app import state
from app.middleware.auth import CurrentUser, get_current_user
from app.models.schemas import IngestedSite, JobStatus, ScrapeRequest, ScrapeResponse
from app.services.ingestion import ingest_site
from app.utils.pinecone_client import get_index
from app.utils.supabase_client import get_svc

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/ingestion/scrape", response_model=ScrapeResponse, summary="Start website ingestion")
async def start_scrape(
    req: ScrapeRequest,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(get_current_user),
) -> ScrapeResponse:
    domain = urlparse(req.url).netloc

    # Register the job in kb_documents so it's visible in the site list
    svc = get_svc()
    doc = await asyncio.to_thread(
        lambda: svc.table("kb_documents")
        .insert({
            "org_id": user.org_id,
            "uploaded_by": user.user_id,
            "source_type": "website",
            "source_url": req.url,
            "file_path": domain,        # store parsed domain for easy lookup
            "status": "processing",
        })
        .execute()
    )
    kb_doc_id = doc.data[0]["id"]

    job: dict = {
        "job_id": kb_doc_id,   # use Supabase doc id so polling survives restarts
        "status": JobStatus.pending,
        "url": req.url,
        "org_id": user.org_id,
        "user_id": user.user_id,
        "kb_doc_id": kb_doc_id,
        "pages_scraped": 0,
        "chunks_indexed": 0,
        "error": None,
    }
    state.jobs[kb_doc_id] = job
    background_tasks.add_task(_run, job)
    return ScrapeResponse(job_id=kb_doc_id, status=JobStatus.pending, url=req.url)


@router.get("/ingestion/scrape/{job_id}", response_model=ScrapeResponse, summary="Poll job status")
async def get_status(
    job_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> ScrapeResponse:
    # In-memory first (live progress); fall back to Supabase after restart
    job = state.jobs.get(job_id)
    if job:
        if job["org_id"] != user.org_id:
            raise HTTPException(status_code=404, detail="Job not found")
        return ScrapeResponse(
            job_id=job["job_id"],
            status=job["status"],
            url=job["url"],
            pages_scraped=job["pages_scraped"],
            chunks_indexed=job["chunks_indexed"],
            message=job.get("error") or "",
        )
    svc = get_svc()
    result = await asyncio.to_thread(
        lambda: svc.table("kb_documents")
        .select("id, source_url, status, chunk_count")
        .eq("id", job_id)
        .eq("org_id", user.org_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Job not found")
    row = result.data[0]
    return ScrapeResponse(
        job_id=row["id"],
        status=row["status"],
        url=row["source_url"] or "",
        chunks_indexed=row["chunk_count"] or 0,
    )


@router.get("/ingestion/sites", response_model=list[IngestedSite], summary="List all ingested websites")
async def list_sites(
    user: CurrentUser = Depends(get_current_user),
) -> list[IngestedSite]:
    svc = get_svc()
    result = await asyncio.to_thread(
        lambda: svc.table("kb_documents")
        .select("id, file_path, source_url, status, chunk_count, created_at")
        .eq("org_id", user.org_id)
        .eq("source_type", "website")
        .order("created_at", desc=True)
        .execute()
    )
    return [
        IngestedSite(
            id=row["id"],
            domain=row["file_path"] or "",
            source_url=row["source_url"] or "",
            status=row["status"],
            chunk_count=row["chunk_count"] or 0,
            created_at=row["created_at"],
        )
        for row in (result.data or [])
    ]


@router.delete(
    "/ingestion/sites/{domain}",
    summary="Delete all ingested vectors for a domain",
)
async def delete_site(
    domain: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    # Remove vectors from Pinecone namespace for this dealership
    index = get_index()
    await asyncio.to_thread(
        index.delete,
        filter={"domain": {"$eq": domain}},
        namespace=user.org_id,
    )

    # Remove the kb_documents record(s) for this domain
    svc = get_svc()
    await asyncio.to_thread(
        lambda: svc.table("kb_documents")
        .delete()
        .eq("org_id", user.org_id)
        .eq("file_path", domain)
        .execute()
    )

    logger.info("Deleted site domain=%s org=%s", domain, user.org_id)
    return {"domain": domain, "deleted": True}


async def _run(job: dict) -> None:
    svc = get_svc()
    job["status"] = JobStatus.running
    try:
        await ingest_site(job)
        job["status"] = JobStatus.completed
        await asyncio.to_thread(
            lambda: svc.table("kb_documents")
            .update({"status": "indexed", "chunk_count": job["chunks_indexed"]})
            .eq("id", job["kb_doc_id"])
            .execute()
        )
    except Exception as exc:
        job["status"] = JobStatus.failed
        job["error"] = str(exc)
        logger.exception("Ingestion failed for job %s", job["job_id"])
        await asyncio.to_thread(
            lambda: svc.table("kb_documents")
            .update({"status": "failed"})
            .eq("id", job["kb_doc_id"])
            .execute()
        )
