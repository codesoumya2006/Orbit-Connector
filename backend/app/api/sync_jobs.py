from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.core.context import OrganizationContext, require_permission
from app.core.database import get_db
from app.schemas.sync_jobs import SyncJobCreate, SyncJobListResponse, SyncJobResponse
from app.services.sync_jobs import (
    cancel_sync_job,
    create_or_get_sync_job,
    get_sync_job,
    list_sync_jobs,
)

router = APIRouter()


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
        items=[
            SyncJobResponse(
                id=j.id,
                organization_id=j.organization_id,
                connection_id=j.connection_id,
                requested_by_user_id=j.requested_by_user_id,
                job_type=j.job_type,
                status=j.status,
                idempotency_key=j.idempotency_key,
                attempt_count=j.attempt_count,
                max_attempts=j.max_attempts,
                created_at=j.created_at,
                updated_at=j.updated_at,
                started_at=j.started_at,
                completed_at=j.completed_at,
                next_retry_at=j.next_retry_at,
                metadata=j.metadata_json or {},
                result=j.result_json or {},
                error=j.error_json or {},
                correlation_id=j.correlation_id,
            )
            for j in items
        ],
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


@router.post("/sync-jobs/{job_id}/cancel", response_model=SyncJobResponse)
def cancel_job(
    job_id: UUID,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    job = cancel_sync_job(db=db, context=context, job_id=job_id)
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

