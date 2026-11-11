from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.core.context import OrganizationContext, require_permission
from app.core.database import get_db
from app.models.entities import SyncJob
from app.schemas.sync_jobs import SyncJobCreate, SyncJobListResponse, SyncJobResponse
from app.services.sync_jobs import (
    cancel_sync_job,
    create_or_get_sync_job,
    get_sync_job,
    list_sync_jobs,
)
from app.workers.local_worker import claim_next_job, process_claimed_job

router = APIRouter()


def _serialize_sync_job(job: SyncJob) -> SyncJobResponse:
    return SyncJobResponse(
        id=job.id,
        organization_id=job.organization_id,
        connection_id=job.connection_id,
        requested_by_user_id=job.requested_by_user_id,
        job_type=job.job_type,
        status=job.status,
        idempotency_key=job.idempotency_key,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        next_retry_at=job.next_retry_at,
        metadata=job.metadata_json or {},
        result=job.result_json or {},
        error=job.error_json or {},
        correlation_id=job.correlation_id,
    )


@router.post("/sync-jobs", response_model=SyncJobResponse)
def create_sync_job(
    payload: SyncJobCreate,
    request: Request,
    response: Response,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    correlation_id = request.headers.get("x-request-id") or request.headers.get("X-Request-ID")
    job, created = create_or_get_sync_job(
        db=db,
        context=context,
        payload=payload,
        correlation_id=correlation_id,
    )
    if created:
        response.status_code = status.HTTP_201_CREATED
    else:
        response.status_code = status.HTTP_200_OK

    return _serialize_sync_job(job)


@router.get("/sync-jobs", response_model=SyncJobListResponse)
def list_jobs(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    items, total = list_sync_jobs(
        db=db,
        context=context,
        status_filter=status,
        limit=limit,
        offset=offset,
    )
    return SyncJobListResponse(
        items=[_serialize_sync_job(j) for j in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/sync-jobs/{job_id}", response_model=SyncJobResponse)
def retrieve_job(
    job_id: UUID,
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    job = get_sync_job(db=db, context=context, job_id=job_id)
    return _serialize_sync_job(job)


@router.post("/sync-jobs/{job_id}/run", response_model=SyncJobResponse)
def run_job_now(
    job_id: UUID,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Execute a queued or retrying sync job immediately within the API process."""
    job = get_sync_job(db=db, context=context, job_id=job_id)
    if job.status in ("queued", "retrying"):
        worker_id = f"api-run-{uuid4().hex[:8]}"
        claimed = claim_next_job(db=db, worker_id=worker_id, job_id=job.id)
        if claimed is not None:
            process_claimed_job(db=db, job=claimed, worker_id=worker_id)
            job = get_sync_job(db=db, context=context, job_id=job_id)
    return _serialize_sync_job(job)


@router.post("/sync-jobs/{job_id}/cancel", response_model=SyncJobResponse)
def cancel_job(
    job_id: UUID,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    job = cancel_sync_job(db=db, context=context, job_id=job_id)
    return _serialize_sync_job(job)

