import concurrent.futures
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import (
    AuditEvent,
    IntegrationConnection,
    Organization,
    OrganizationMembership,
    SyncJob,
    User,
)
from app.workers.local_worker import (
    DeterministicTestAdapter,
    SyncWorkerError,
    claim_next_job,
    process_claimed_job,
)


def get_test_engine():
    """Returns a PostgreSQL engine if TEST_DATABASE_URL is set and reachable;

    otherwise creates a dedicated file-backed SQLite database in WAL mode to allow
    multi-connection concurrent transactions.
    """
    pg_url = os.getenv("TEST_DATABASE_URL")
    if pg_url and pg_url.startswith("postgresql"):
        try:
            engine = create_engine(pg_url, pool_size=10, max_overflow=5)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return engine, True, None
        except Exception as exc:
            pass

    # Fallback to local SQLite file in WAL mode for multi-connection concurrent simulation
    db_file = Path.cwd() / f".concurrency-test-{uuid4().hex}.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"timeout": 30},
    )
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA busy_timeout=5000"))
    return engine, False, db_file


@pytest.fixture
def concurrency_env():
    engine, is_postgres, db_file = get_test_engine()
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # Seed baseline organization, user, and connection
    with session_factory() as db:
        user = User(
            id=uuid4(),
            firebase_uid="uid-concurrent",
            email="concurrent@orbit.test",
            email_verified=True,
        )
        org = Organization(id=uuid4(), name="Concurrency Org")
        db.add_all([user, org])
        db.flush()

        db.add(OrganizationMembership(user_id=user.id, organization_id=org.id, role="owner"))
        conn = IntegrationConnection(
            id=f"conn-concurrent-{uuid4().hex[:8]}",
            organization_id=org.id,
            provider_key="google_analytics",
            display_name="GA4 Concurrent",
            status="active",
            connection_type="oauth",
        )
        db.add(conn)
        db.commit()

        org_id = org.id
        user_id = user.id
        conn_id = conn.id

    yield {
        "engine": engine,
        "is_postgres": is_postgres,
        "session_factory": session_factory,
        "org_id": org_id,
        "user_id": user_id,
        "conn_id": conn_id,
    }

    engine.dispose()
    if db_file is not None and db_file.exists():
        for f in db_file.parent.glob(f"{db_file.name}*"):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass


def test_concurrent_worker_claiming_mutual_exclusion(concurrency_env):
    """Verify that when multiple concurrent workers attempt to claim a single queued job,

    only one worker successfully claims it, and the other receives None.
    """
    session_factory = concurrency_env["session_factory"]
    org_id = concurrency_env["org_id"]
    conn_id = concurrency_env["conn_id"]
    user_id = concurrency_env["user_id"]

    # Insert 1 queued job
    with session_factory() as db:
        job = SyncJob(
            id=uuid4(),
            organization_id=org_id,
            connection_id=conn_id,
            requested_by_user_id=user_id,
            status="queued",
            attempt_count=0,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    # Function executed concurrently by 2 worker threads
    def worker_claim(worker_name: str) -> str | None:
        with session_factory() as db:
            claimed = claim_next_job(db, worker_id=worker_name, lease_duration_seconds=30)
            if claimed:
                return worker_name
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker_claim, "worker-alpha")
        f2 = executor.submit(worker_claim, "worker-bravo")
        res1 = f1.result()
        res2 = f2.result()

    winners = [r for r in (res1, res2) if r is not None]
    assert len(winners) == 1, f"Expected exactly 1 worker to claim the job, got: {winners}"
    winner = winners[0]

    # Verify database state
    with session_factory() as db:
        claimed_job = db.query(SyncJob).filter_by(id=job_id).one()
        assert claimed_job.status == "running"
        assert claimed_job.lease_token == winner
        assert claimed_job.attempt_count == 1


def test_concurrent_cancellation_versus_worker_completion(concurrency_env):
    """Verify that if a job is cancelled while a worker is actively executing,

    the worker cannot overwrite the cancelled status with 'succeeded'.
    """
    session_factory = concurrency_env["session_factory"]
    org_id = concurrency_env["org_id"]
    conn_id = concurrency_env["conn_id"]
    user_id = concurrency_env["user_id"]

    # Seed job
    with session_factory() as db:
        job = SyncJob(
            id=uuid4(),
            organization_id=org_id,
            connection_id=conn_id,
            requested_by_user_id=user_id,
            status="queued",
        )
        db.add(job)
        db.commit()
        job_id = job.id

    # Worker 1 claims job
    with session_factory() as db_worker:
        claimed = claim_next_job(db_worker, worker_id="worker-canc-test", job_id=job_id)
        assert claimed is not None
        assert claimed.status == "running"

        # Simulating external cancellation during worker execution
        with session_factory() as db_canceller:
            to_cancel = db_canceller.query(SyncJob).filter_by(id=job_id).one()
            now = datetime.now(timezone.utc)
            to_cancel.status = "cancelled"
            to_cancel.completed_at = now
            to_cancel.lease_token = None
            to_cancel.lease_expires_at = None
            db_canceller.commit()

        # Worker finishes execution and attempts to finalize
        adapter = DeterministicTestAdapter(default_result={"synced_records": 42})
        with pytest.raises(SyncWorkerError) as exc_info:
            process_claimed_job(db_worker, claimed, adapter, worker_id="worker-canc-test")
        assert "lost active lease" in str(exc_info.value)

    # Verify state in DB remains strictly cancelled
    with session_factory() as db:
        final_job = db.query(SyncJob).filter_by(id=job_id).one()
        assert final_job.status == "cancelled"
        assert final_job.result_json == {}

        # No success audit event
        succ_audit = (
            db.query(AuditEvent)
            .filter_by(target_id=str(job_id), event_type="sync_job.succeeded")
            .first()
        )
        assert succ_audit is None


def test_transaction_rollback_preserves_database_consistency(concurrency_env):
    """Verify that if an error occurs within a transaction (e.g.

    audit failure or constraint violation), all mutations roll back cleanly.
    """
    session_factory = concurrency_env["session_factory"]
    org_id = concurrency_env["org_id"]
    conn_id = concurrency_env["conn_id"]
    user_id = concurrency_env["user_id"]

    job_id = uuid4()
    with session_factory() as db:
        # Begin transaction
        try:
            job = SyncJob(
                id=job_id,
                organization_id=org_id,
                connection_id=conn_id,
                requested_by_user_id=user_id,
                status="queued",
            )
            db.add(job)
            db.flush()

            # Simulate an error in transaction before commit
            raise RuntimeError("Simulated transaction fault")
        except RuntimeError:
            db.rollback()

    # Verify with independent session that job_id was not committed
    with session_factory() as db:
        row = db.query(SyncJob).filter_by(id=job_id).first()
        assert row is None

