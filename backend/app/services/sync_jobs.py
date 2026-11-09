from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.context import OrganizationContext
from app.models.entities import AuditEvent, IntegrationConnection, SyncJob
from app.schemas.sync_jobs import SyncJobCreate

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
VALID_STATES = frozenset({"queued", "running", "retrying", "succeeded", "failed", "cancelled"})


def create_or_get_sync_job(
    db: Session,
    context: OrganizationContext,
    payload: SyncJobCreate,
    correlation_id: str | None = None,
) -> tuple[SyncJob, bool]:
    # 1. Verify connection belongs to the caller's organization
    connection = (
        db.query(IntegrationConnection)
        .filter_by(id=payload.connection_id, organization_id=context.organization_id)
        .first()
    )
    if not connection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Connection not found in the current organization.",
        )

    # 2. Check idempotency key if provided
    if payload.idempotency_key and payload.idempotency_key.strip():
        existing_job = (
            db.query(SyncJob)
            .filter_by(
                organization_id=context.organization_id,
                idempotency_key=payload.idempotency_key.strip(),
            )
            .first()
        )
        if existing_job:
            return existing_job, False

    # 3. Create new job
    now = datetime.now(timezone.utc)
    job = SyncJob(
        id=uuid4(),
        organization_id=context.organization_id,
        connection_id=connection.id,
        requested_by_user_id=context.user_id,
        job_type=payload.job_type,
        status="queued",
        idempotency_key=payload.idempotency_key.strip() if payload.idempotency_key else None,
        attempt_count=0,
        max_attempts=3,
        created_at=now,
        updated_at=now,
        metadata_json=payload.metadata or {},
        result_json={},
        error_json={},
        correlation_id=correlation_id,
    )

    audit = AuditEvent(
        actor_user_id=context.user_id,
        organization_id=context.organization_id,
        event_type="sync_job.created",
        target_type="sync_job",
        target_id=str(job.id),
        created_at=now,
        metadata_json={
            "job_type": job.job_type,
            "connection_id": job.connection_id,
            "idempotency_key": job.idempotency_key,
        },
    )

    try:
        db.add(job)
        db.flush()
        db.add(audit)
        db.commit()
        db.refresh(job)
        return job, True
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create sync job.",
        ) from exc


def get_sync_job(
    db: Session,
    context: OrganizationContext,
    job_id: UUID,
) -> SyncJob:
    job = (
        db.query(SyncJob)
        .filter_by(id=job_id, organization_id=context.organization_id)
        .first()
    )
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sync job not found.",
        )
    return job


def list_sync_jobs(
    db: Session,
    context: OrganizationContext,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[SyncJob], int]:
    query = db.query(SyncJob).filter_by(organization_id=context.organization_id)
    if status_filter:
        if status_filter not in VALID_STATES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status filter '{status_filter}'.",
            )
        query = query.filter_by(status=status_filter)

    total = query.count()
    items = (
        query.order_by(SyncJob.created_at.desc())
        .offset(max(0, offset))
        .limit(min(max(1, limit), 100))
        .all()
    )
    return items, total


def cancel_sync_job(
    db: Session,
    context: OrganizationContext,
    job_id: UUID,
) -> SyncJob:
    job = (
        db.query(SyncJob)
        .filter_by(id=job_id, organization_id=context.organization_id)
        .with_for_update()
        .first()
    )
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sync job not found.",
        )

    if job.status in TERMINAL_STATES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel sync job in terminal state '{job.status}'.",
        )

    previous_status = job.status
    now = datetime.now(timezone.utc)
    job.status = "cancelled"
    job.completed_at = now
    job.updated_at = now
    job.lease_token = None
    job.lease_expires_at = None

    audit = AuditEvent(
        actor_user_id=context.user_id,
        organization_id=context.organization_id,
        event_type="sync_job.cancelled",
        target_type="sync_job",
        target_id=str(job.id),
        created_at=now,
        metadata_json={
            "previous_status": previous_status,
            "job_type": job.job_type,
            "connection_id": job.connection_id,
        },
    )

    try:
        db.add(audit)
        db.commit()
        db.refresh(job)
        return job
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cancel sync job.",
        ) from exc

