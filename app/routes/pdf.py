import asyncio
import logging
import re
import uuid
from pathlib import PurePosixPath

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile

from app import state
from app.config import get_settings
from app.middleware.auth import CurrentUser, get_current_user
from app.models.schemas import JobStatus, PdfDocument, PdfUploadResponse
from app.services.pdf_ingestion import delete_pdf_vectors, ingest_pdf
from app.utils.supabase_client import get_svc

router = APIRouter(tags=["Ingestion"])
logger = logging.getLogger(__name__)


@router.post("/ingestion/pdf", response_model=PdfUploadResponse, summary="Upload and index a PDF (one per org)")
async def upload_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(get_current_user),
) -> PdfUploadResponse:
    settings = get_settings()

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Only PDF files are accepted.")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=422, detail="Uploaded file is empty.")
    if len(pdf_bytes) > settings.pdf_max_size_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File exceeds {settings.pdf_max_size_mb} MB limit.")

    svc = get_svc()

    safe_name = re.sub(r"[^a-zA-Z0-9._\-]", "_", file.filename)
    storage_path = f"orgs/{user.org_id}/docs/{uuid.uuid4()}_{safe_name}"
    try:
        await asyncio.to_thread(
            lambda: svc.storage.from_("dealership-files")
            .upload(storage_path, pdf_bytes, {"content-type": "application/pdf"})
        )
    except Exception as exc:
        logger.warning("Storage upload failed (non-fatal): %s", exc)

    doc = await asyncio.to_thread(
        lambda: svc.table("kb_documents")
        .insert({
            "org_id": user.org_id,
            "uploaded_by": user.user_id,
            "source_type": "pdf",
            "file_path": storage_path,
            "status": "processing",
        })
        .execute()
    )
    kb_doc_id = doc.data[0]["id"]

    job: dict = {
        "job_id": kb_doc_id,
        "status": JobStatus.pending,
        "org_id": user.org_id,
        "user_id": user.user_id,
        "kb_doc_id": kb_doc_id,
        "filename": safe_name,
        "chunks_indexed": 0,
        "error": None,
    }
    state.jobs[kb_doc_id] = job
    background_tasks.add_task(_run, job, pdf_bytes)
    return PdfUploadResponse(job_id=kb_doc_id, status=JobStatus.pending, filename=safe_name)


@router.get("/ingestion/pdf/status/{job_id}", response_model=PdfUploadResponse, summary="Poll PDF ingestion status")
async def pdf_status(
    job_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> PdfUploadResponse:
    job = state.jobs.get(job_id)
    if job:
        if job["org_id"] != user.org_id:
            raise HTTPException(status_code=404, detail="Job not found.")
        return PdfUploadResponse(
            job_id=job["job_id"],
            status=job["status"],
            filename=job["filename"],
            chunks_indexed=job["chunks_indexed"],
            message=job.get("error") or "",
        )

    svc = get_svc()
    result = await asyncio.to_thread(
        lambda: svc.table("kb_documents")
        .select("id, file_path, status, chunk_count")
        .eq("id", job_id).eq("org_id", user.org_id).eq("source_type", "pdf")
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Job not found.")
    row = result.data[0]
    return PdfUploadResponse(
        job_id=row["id"],
        status=row["status"],
        filename=row.get("file_path", "").split("/")[-1],
        chunks_indexed=row.get("chunk_count") or 0,
    )


@router.get("/ingestion/pdfs", response_model=list[PdfDocument], summary="List all PDFs uploaded by this org")
async def list_pdfs(user: CurrentUser = Depends(get_current_user)) -> list[PdfDocument]:
    svc = get_svc()
    result = await asyncio.to_thread(
        lambda: svc.table("kb_documents")
        .select("id, file_path, status, chunk_count, created_at")
        .eq("org_id", user.org_id).eq("source_type", "pdf")
        .order("created_at", desc=True)
        .execute()
    )
    return [
        PdfDocument(
            id=row["id"],
            title=PurePosixPath(row.get("file_path", "").split("/")[-1]).stem.replace("_", " ").replace("-", " ").title() or None,
            filename=row.get("file_path", "").split("/")[-1],
            status=row["status"],
            chunk_count=row.get("chunk_count") or 0,
            created_at=row["created_at"],
        )
        for row in (result.data or [])
    ]


@router.get("/ingestion/pdf", response_model=PdfDocument, summary="Get this org's current PDF")
async def get_pdf(user: CurrentUser = Depends(get_current_user)) -> PdfDocument:
    svc = get_svc()
    result = await asyncio.to_thread(
        lambda: svc.table("kb_documents")
        .select("id, file_path, status, chunk_count, created_at")
        .eq("org_id", user.org_id).eq("source_type", "pdf")
        .order("created_at", desc=True).limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="No PDF uploaded yet.")
    row = result.data[0]
    filename = row.get("file_path", "").split("/")[-1]
    return PdfDocument(
        id=row["id"],
        title=PurePosixPath(filename).stem.replace("_", " ").replace("-", " ").title() or None,
        filename=filename,
        status=row["status"],
        chunk_count=row.get("chunk_count") or 0,
        created_at=row["created_at"],
    )


@router.delete("/ingestion/pdf", summary="Delete this org's PDF — removes vectors, storage, and DB rows")
async def delete_pdf(user: CurrentUser = Depends(get_current_user)) -> dict:
    svc = get_svc()
    result = await asyncio.to_thread(
        lambda: svc.table("kb_documents")
        .select("id, file_path")
        .eq("org_id", user.org_id).eq("source_type", "pdf")
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="No PDF found for this org.")
    for row in result.data:
        await delete_pdf_vectors(row["id"], user.org_id)
        await _storage_delete(svc, row.get("file_path") or "")
        state.jobs.pop(row["id"], None)
    logger.info("PDF deleted | org=%s", user.org_id)
    return {"deleted": True}


async def _run(job: dict, pdf_bytes: bytes) -> None:
    svc = get_svc()
    job["status"] = JobStatus.running
    try:
        await ingest_pdf(job, pdf_bytes)
        job["status"] = JobStatus.completed
        await asyncio.to_thread(
            lambda: svc.table("kb_documents")
            .update({"status": "indexed", "chunk_count": job["chunks_indexed"]})
            .eq("id", job["kb_doc_id"]).execute()
        )
        logger.info("PDF indexed | job=%s chunks=%d", job["job_id"], job["chunks_indexed"])
    except Exception as exc:
        job["status"] = JobStatus.failed
        job["error"] = str(exc)
        logger.exception("PDF ingestion failed | job=%s", job["job_id"])
        await asyncio.to_thread(
            lambda: svc.table("kb_documents")
            .update({"status": "failed", "error_msg": str(exc)[:500]})
            .eq("id", job["kb_doc_id"]).execute()
        )


async def _storage_delete(svc, path: str) -> None:
    if not path:
        return
    try:
        await asyncio.to_thread(lambda: svc.storage.from_("dealership-files").remove([path]))
    except Exception as exc:
        logger.warning("Storage delete failed (non-fatal): %s", exc)
