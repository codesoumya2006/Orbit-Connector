import argparse
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import or_
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import engine
from app.models.entities import AuditEvent, IntegrationConnection, SyncJob

logger = logging.getLogger("orbit.worker")


class SyncWorkerError(Exception):
    """Base error for sync worker operations."""


class RetryableSyncError(SyncWorkerError):
    """Temporary failure eligible for retry with backoff."""


class PermanentSyncError(SyncWorkerError):
    """Fatal failure that should not be retried."""


class SyncAdapter(Protocol):
    def execute(self, connection: IntegrationConnection, job: SyncJob) -> dict[str, Any]:
        """Execute synchronization for a specific connection and job.

        Note on Cancellation Safety:
        Database rollback protects internal system state (job status, result, lease, audit log).
        However, arbitrary external provider side effects (e.g. remote API mutations, webhooks,
        or third-party data processing) executed prior to or during cancellation cannot be rolled
        back automatically by the hub. Adapters should be designed to be idempotent and checkpointed.
        """
        ...


class DeterministicTestAdapter:
    """Configurable test adapter to verify success, retry, and fatal paths deterministically."""

    def __init__(self, behaviors: dict[str, Any] | None = None, default_result: dict[str, Any] | None = None):
        self.behaviors = behaviors or {}
        self.default_result = default_result or {"records_synced": 10, "status": "completed"}
        self.call_log: list[tuple[str, str]] = []

    def execute(self, connection: IntegrationConnection, job: SyncJob) -> dict[str, Any]:
        self.call_log.append((connection.id, str(job.id)))
        behavior = self.behaviors.get(connection.id, self.default_result)

        if isinstance(behavior, Exception):
            raise behavior

        if isinstance(behavior, dict) and behavior.get("error"):
            err_msg = behavior["error"]
            if behavior.get("retryable", False):
                raise RetryableSyncError(err_msg)
            raise PermanentSyncError(err_msg)

        return behavior


def calculate_backoff_seconds(attempt: int, base_seconds: int = 2, max_seconds: int = 300) -> int:
    return min(max_seconds, (2 ** max(0, attempt - 1)) * base_seconds)


def claim_next_job(
    db: Session,
    worker_id: str,
    lease_duration_seconds: int = 60,
    job_id: UUID | None = None,
) -> SyncJob | None:
    now = datetime.now(timezone.utc)

    query = db.query(SyncJob)

    if job_id:
        query = query.filter(SyncJob.id == job_id)
    else:
        # Eligible conditions:
        # 1. queued jobs
        # 2. retrying jobs whose next_retry_at has elapsed
        # 3. running jobs whose lease has expired (crash recovery)
        is_queued = SyncJob.status == "queued"
        is_retrying_ready = (SyncJob.status == "retrying") & (
            (SyncJob.next_retry_at == None) | (SyncJob.next_retry_at <= now)
        )
        is_lease_expired = (SyncJob.status == "running") & (
            (SyncJob.lease_expires_at != None) & (SyncJob.lease_expires_at < now)
        )

        query = query.filter(or_(is_queued, is_retrying_ready, is_lease_expired))

    # Apply row-level lock where dialect supports it
    try:
        job = query.with_for_update().order_by(SyncJob.created_at.asc()).first()
    except Exception:
        # Fallback for dialects without FOR UPDATE support in tests
        job = query.order_by(SyncJob.created_at.asc()).first()

    if not job:
        return None

    # Check state again to prevent race
    if job.status not in ("queued", "retrying", "running"):
        return None

    if job.status == "running" and job.lease_expires_at:
        lease_exp = job.lease_expires_at
        if lease_exp.tzinfo is None:
            lease_exp = lease_exp.replace(tzinfo=timezone.utc)
        if lease_exp >= now:
            return None

    # Transition to running
    job.status = "running"
    job.attempt_count += 1
    job.lease_token = worker_id
    job.lease_expires_at = now + timedelta(seconds=lease_duration_seconds)
    if not job.started_at:
        job.started_at = now
    job.updated_at = now

    db.commit()
    db.refresh(job)
    return job


def _verify_active_lease(db: Session, job_id: UUID, worker_id: str | None) -> SyncJob:
    """Acquire row lock and verify that the worker still holds an active running lease.

    If the job was cancelled or its expired lease was claimed by another worker,
    this raises SyncWorkerError and aborts state transitions to prevent stale overwrites.
    """
    # Force query to bypass session identity-map caching and retrieve committed DB state
    query = db.query(SyncJob).filter_by(id=job_id).execution_options(populate_existing=True)
    try:
        current_job = query.with_for_update().first()
    except Exception:
        current_job = query.first()

    if not current_job:
        raise SyncWorkerError(f"Job '{job_id}' no longer exists in database.")

    try:
        db.refresh(current_job)
    except Exception:
        pass

    if worker_id is not None:
        if current_job.status != "running" or current_job.lease_token != worker_id:
            db.rollback()
            raise SyncWorkerError(
                f"Worker '{worker_id}' lost active lease on job '{job_id}' "
                f"(current status: '{current_job.status}', lease_token: '{current_job.lease_token}'). "
                f"Aborting update to prevent stale overwrite."
            )

        if current_job.lease_expires_at:
            lease_exp = current_job.lease_expires_at
            if lease_exp.tzinfo is None:
                lease_exp = lease_exp.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if lease_exp < now:
                db.rollback()
                raise SyncWorkerError(
                    f"Worker '{worker_id}' lease expired at {lease_exp.isoformat()} for job '{job_id}' (now: {now.isoformat()}). "
                    f"Aborting update to prevent stale overwrite."
                )

    return current_job


def process_claimed_job(
    db: Session,
    job: SyncJob,
    adapter: SyncAdapter | None = None,
    worker_id: str = "default-worker",
) -> SyncJob:
    now = datetime.now(timezone.utc)

    # 1. Lease token check and fresh DB verification
    job = _verify_active_lease(db, job.id, worker_id)

    # 2. Derive organization ID from persisted job data
    org_id = job.organization_id

    # 3. Verify connection belongs to the exact same organization
    connection = (
        db.query(IntegrationConnection)
        .filter_by(id=job.connection_id, organization_id=org_id)
        .first()
    )

    if not connection:
        return _fail_job(
            db,
            job,
            error_message="Referenced connection not found or does not belong to the job organization.",
            retryable=False,
            worker_id=worker_id,
            now=now,
        )

    # 4. Check connection eligibility
    if connection.status == "archived" or connection.status == "disabled":
        return _fail_job(
            db,
            job,
            error_message=f"Connection is in '{connection.status}' state and not eligible for execution.",
            retryable=False,
            worker_id=worker_id,
            now=now,
        )

    # 5. Resolve adapter if not explicitly supplied
    active_adapter = adapter
    if active_adapter is None:
        if connection.provider_key == "google_analytics":
            from app.providers.google_analytics.adapter import GoogleAnalyticsAdapter
            active_adapter = GoogleAnalyticsAdapter(db=db)
        elif connection.provider_key == "google_ads":
            from app.providers.google_ads.adapter import GoogleAdsAdapter
            active_adapter = GoogleAdsAdapter(db=db)
        elif connection.provider_key == "meta_ads":
            from app.providers.meta_ads.adapter import MetaAdsAdapter
            active_adapter = MetaAdsAdapter(db=db)
        elif connection.provider_key == "supermetrics":
            from app.providers.supermetrics.adapter import SupermetricsAdapter
            active_adapter = SupermetricsAdapter(db=db)
        else:
            return _fail_job(
                db,
                job,
                error_message=f"No provider sync adapter registered for provider '{connection.provider_key}'.",
                retryable=False,
                worker_id=worker_id,
                now=now,
            )

    # 6. Execute through adapter
    try:
        raw_result = active_adapter.execute(connection, job)
        sanitized_result = _sanitize_dict(raw_result)
        return _succeed_job(db, job, sanitized_result, worker_id, now)
    except RetryableSyncError as exc:
        sanitized_err = _sanitize_string(str(exc))
        if job.attempt_count < job.max_attempts:
            return _retry_job(db, job, sanitized_err, worker_id, now)
        return _fail_job(
            db,
            job,
            error_message=f"Maximum attempts ({job.max_attempts}) reached. Last error: {sanitized_err}",
            retryable=False,
            worker_id=worker_id,
            now=now,
        )
    except PermanentSyncError as exc:
        return _fail_job(
            db,
            job,
            error_message=_sanitize_string(str(exc)),
            retryable=False,
            worker_id=worker_id,
            now=now,
        )
    except SyncWorkerError:
        # Re-raise lease loss or state conflict without converting to fatal error
        raise
    except Exception as exc:
        return _fail_job(
            db,
            job,
            error_message=f"Unexpected execution failure: {type(exc).__name__}: {_sanitize_string(str(exc))}",
            retryable=False,
            worker_id=worker_id,
            now=now,
        )




def _succeed_job(db: Session, job: SyncJob, result: dict[str, Any], worker_id: str, now: datetime) -> SyncJob:
    current_job = _verify_active_lease(db, job.id, worker_id)

    current_job.status = "succeeded"
    current_job.completed_at = now
    current_job.updated_at = now
    current_job.result_json = result
    current_job.lease_token = None
    current_job.lease_expires_at = None

    audit = AuditEvent(
        actor_user_id=current_job.requested_by_user_id,
        organization_id=current_job.organization_id,
        event_type="sync_job.succeeded",
        target_type="sync_job",
        target_id=str(current_job.id),
        created_at=now,
        metadata_json={
            "job_type": current_job.job_type,
            "connection_id": current_job.connection_id,
            "attempt_count": current_job.attempt_count,
        },
    )

    try:
        db.add(audit)
        db.commit()
        db.refresh(current_job)
        return current_job
    except Exception as exc:
        db.rollback()
        raise SyncWorkerError(f"Failed to commit job success and audit event: {exc}") from exc


def _retry_job(db: Session, job: SyncJob, error_message: str, worker_id: str, now: datetime) -> SyncJob:
    current_job = _verify_active_lease(db, job.id, worker_id)

    backoff_seconds = calculate_backoff_seconds(current_job.attempt_count)
    next_retry = now + timedelta(seconds=backoff_seconds)

    current_job.status = "retrying"
    current_job.updated_at = now
    current_job.next_retry_at = next_retry
    current_job.error_json = {
        "message": error_message,
        "retryable": True,
        "attempt": current_job.attempt_count,
    }
    current_job.lease_token = None
    current_job.lease_expires_at = None

    try:
        db.commit()
        db.refresh(current_job)
        return current_job
    except Exception as exc:
        db.rollback()
        raise SyncWorkerError(f"Failed to commit job retry: {exc}") from exc


def _fail_job(
    db: Session,
    job: SyncJob,
    error_message: str,
    retryable: bool,
    worker_id: str | None,
    now: datetime,
) -> SyncJob:
    current_job = _verify_active_lease(db, job.id, worker_id)

    current_job.status = "failed"
    current_job.completed_at = now
    current_job.updated_at = now
    current_job.error_json = {
        "message": error_message,
        "retryable": retryable,
        "attempt": current_job.attempt_count,
    }
    current_job.lease_token = None
    current_job.lease_expires_at = None

    audit = AuditEvent(
        actor_user_id=current_job.requested_by_user_id,
        organization_id=current_job.organization_id,
        event_type="sync_job.failed",
        target_type="sync_job",
        target_id=str(current_job.id),
        created_at=now,
        metadata_json={
            "job_type": current_job.job_type,
            "connection_id": current_job.connection_id,
            "error_message": error_message,
            "attempt_count": current_job.attempt_count,
        },
    )

    try:
        db.add(audit)
        db.commit()
        db.refresh(current_job)
        return current_job
    except Exception as exc:
        db.rollback()
        raise SyncWorkerError(f"Failed to commit job failure and audit event: {exc}") from exc


_SENSITIVE_PATTERNS = [
    (re.compile(r"Bearer\s+[a-zA-Z0-9_\-\.]+", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"(api[_-]?key|secret|password|token|access[_-]?token|refresh[_-]?token)=([^\s&;]+)", re.IGNORECASE), r"\1=[REDACTED]"),
    (re.compile(r"(['\"]?(?:api[_-]?key|secret|password|token|access[_-]?token|refresh[_-]?token)['\"]?\s*:\s*['\"])[^'\"]+(['\"])", re.IGNORECASE), r"\1[REDACTED]\2"),
]


def _sanitize_string(val: str) -> str:
    cleaned = str(val)
    for pattern, replacement in _SENSITIVE_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned[:500].strip()


def _sanitize_dict(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"data": _sanitize_string(str(data))}

    sensitive_keys = {
        "secret", "token", "password", "api_key", "access_token",
        "refresh_token", "key", "authorization", "credential"
    }

    sanitized: dict[str, Any] = {}
    for k, v in data.items():
        k_lower = str(k).lower()
        if any(bad in k_lower for bad in sensitive_keys):
            continue
        if isinstance(v, str):
            sanitized[k] = _sanitize_string(v)
        elif isinstance(v, (int, float, bool)) or v is None:
            sanitized[k] = v
        elif isinstance(v, dict):
            sanitized[k] = _sanitize_dict(v)
        elif isinstance(v, list):
            sanitized[k] = [
                _sanitize_dict(item) if isinstance(item, dict)
                else _sanitize_string(item) if isinstance(item, str)
                else item
                for item in v
            ]
    return sanitized


def run_worker_loop(
    poll_interval_seconds: float = 2.0,
    worker_id: str | None = None,
    single_run: bool = False,
    lease_duration_seconds: int = 60,
    max_jobs: int | None = None,
) -> int:
    """Run the local worker loop, polling for and processing eligible sync jobs.

    - Single-job transactional claim with row-level locking.
    - Preserves lease ownership, bounded retries, and tenant isolation.
    - Handles graceful termination upon SIGINT / SIGTERM without aborting in-flight work.
    - Avoids busy-polling via controlled sleep intervals.
    """
    if worker_id is None:
        worker_id = f"local-worker-{uuid4().hex[:8]}"

    shutdown_requested = False

    def handle_signal(sig, frame):
        nonlocal shutdown_requested
        logger.info("Shutdown signal received (%s). Completing active work and exiting...", sig)
        shutdown_requested = True

    try:
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
    except (ValueError, AttributeError):
        pass  # In non-main threads or specific Windows environments

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    jobs_processed = 0
    logger.info("Local sync worker '%s' started. Polling interval: %.1fs", worker_id, poll_interval_seconds)

    while not shutdown_requested:
        if max_jobs is not None and jobs_processed >= max_jobs:
            logger.info("Processed maximum requested jobs (%d). Exiting loop.", max_jobs)
            break

        db = SessionLocal()
        try:
            job = claim_next_job(
                db=db,
                worker_id=worker_id,
                lease_duration_seconds=lease_duration_seconds,
            )
            if job:
                logger.info(
                    "Worker '%s' claimed job '%s' (type: %s, org: %s, attempt: %d)",
                    worker_id,
                    job.id,
                    job.job_type,
                    job.organization_id,
                    job.attempt_count,
                )
                try:
                    processed_job = process_claimed_job(
                        db=db,
                        job=job,
                        worker_id=worker_id,
                    )
                    jobs_processed += 1
                    logger.info(
                        "Job '%s' finished with status '%s'",
                        processed_job.id,
                        processed_job.status,
                    )
                except Exception as exc:
                    logger.error("Error processing job '%s': %s", job.id, exc, exc_info=True)

                # Immediately attempt to claim next job without waiting
                continue
        except Exception as exc:
            logger.error("Worker loop iteration error: %s", exc, exc_info=True)
        finally:
            db.close()

        if single_run:
            logger.info("Single-run execution finished.")
            break

        # Sleep in short slices to process shutdown signals promptly
        sleep_slices = max(1, int(poll_interval_seconds / 0.2))
        slice_duration = poll_interval_seconds / sleep_slices
        for _ in range(sleep_slices):
            if shutdown_requested:
                break
            time.sleep(slice_duration)

    logger.info("Worker '%s' shutdown complete. Total jobs processed: %d.", worker_id, jobs_processed)
    return jobs_processed


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Orbit Connector Hub - Local Sync Worker")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds to wait between polls when no jobs are available (default: 2.0)",
    )
    parser.add_argument(
        "--worker-id",
        type=str,
        default=None,
        help="Unique worker identifier (defaults to autogenerated ID)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single pass and exit immediately",
    )
    args = parser.parse_args()

    run_worker_loop(
        poll_interval_seconds=args.poll_interval,
        worker_id=args.worker_id,
        single_run=args.once,
    )

