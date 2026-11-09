from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base, get_db
from app.core.firebase_auth import AuthenticatedPrincipal, get_current_principal
from app.main import app
from app.models.entities import (
    AuditEvent,
    IntegrationConnection,
    Organization,
    OrganizationMembership,
    SyncJob,
    User,
)
from app.schemas.sync_jobs import SyncJobCreate
from app.services.sync_jobs import (
    cancel_sync_job,
    create_or_get_sync_job,
    get_sync_job,
    list_sync_jobs,
)
from app.workers.local_worker import (
    DeterministicTestAdapter,
    PermanentSyncError,
    RetryableSyncError,
    calculate_backoff_seconds,
    claim_next_job,
    process_claimed_job,
)


@pytest.fixture
def sync_env(monkeypatch):
    monkeypatch.setattr(settings, "credential_encryption_key", Fernet.generate_key().decode())
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        owner_user = User(
            id=uuid4(),
            firebase_uid="uid-sync-owner",
            email="owner@orbit.test",
            email_verified=True,
        )
        viewer_user = User(
            id=uuid4(),
            firebase_uid="uid-sync-viewer",
            email="viewer@orbit.test",
            email_verified=True,
        )
        db.add_all([owner_user, viewer_user])
        db.flush()

        org_a = Organization(id=uuid4(), name="Org Alpha")
        org_b = Organization(id=uuid4(), name="Org Beta")
        db.add_all([org_a, org_b])
        db.flush()

        db.add(OrganizationMembership(user_id=owner_user.id, organization_id=org_a.id, role="owner"))
        db.add(OrganizationMembership(user_id=viewer_user.id, organization_id=org_a.id, role="viewer"))

        # Seed connection in Org A
        conn_a = IntegrationConnection(
            id="conn-alpha-1",
            organization_id=org_a.id,
            provider_key="google_analytics",
            display_name="GA4 Alpha",
            status="active",
            connection_type="oauth",
        )
        # Seed connection in Org B (different tenant)
        conn_b = IntegrationConnection(
            id="conn-beta-1",
            organization_id=org_b.id,
            provider_key="google_ads",
            display_name="Ads Beta",
            status="active",
            connection_type="oauth",
        )
        db.add_all([conn_a, conn_b])
        db.commit()

        org_a_id = org_a.id
        org_b_id = org_b.id
        owner_id = owner_user.id
        viewer_id = viewer_user.id

    principal_state = {
        "user_id": owner_id,
        "email": "owner@orbit.test",
        "uid": "uid-sync-owner",
    }

    def override_get_db():
        with session_factory() as db:
            yield db

    def override_get_principal():
        return AuthenticatedPrincipal(
            uid=principal_state["uid"],
            auth_time=datetime.now(timezone.utc),
            user=User(
                id=principal_state["user_id"],
                firebase_uid=principal_state["uid"],
                email=principal_state["email"],
                email_verified=True,
            ),
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_principal] = override_get_principal

    with TestClient(app, raise_server_exceptions=False) as client:
        yield {
            "client": client,
            "engine": engine,
            "session_factory": session_factory,
            "org_a_id": org_a_id,
            "org_b_id": org_b_id,
            "owner_id": owner_id,
            "viewer_id": viewer_id,
            "principal_state": principal_state,
        }

    app.dependency_overrides.clear()
    engine.dispose()


def test_job_creation_and_retrieval(sync_env):
    client = sync_env["client"]
    org_id = str(sync_env["org_a_id"])

    # Create job
    res = client.post(
        "/api/v1/sync-jobs",
        headers={"X-Organization-ID": org_id},
        json={
            "connection_id": "conn-alpha-1",
            "job_type": "full_sync",
            "metadata": {"dataset": "analytics_sessions"},
        },
    )
    assert res.status_code == 201
    data = res.json()
    job_id = data["id"]
    assert data["connection_id"] == "conn-alpha-1"
    assert data["status"] == "queued"
    assert data["attempt_count"] == 0
    assert data["metadata"]["dataset"] == "analytics_sessions"

    # Verify audit event for sync_job.created
    with sync_env["session_factory"]() as db:
        audit = (
            db.query(AuditEvent)
            .filter_by(target_id=job_id, event_type="sync_job.created")
            .first()
        )
        assert audit is not None
        assert audit.actor_user_id == sync_env["owner_id"]
        assert audit.metadata_json["job_type"] == "full_sync"

    # Retrieve job
    get_res = client.get(
        f"/api/v1/sync-jobs/{job_id}",
        headers={"X-Organization-ID": org_id},
    )
    assert get_res.status_code == 200
    assert get_res.json()["id"] == job_id


def test_idempotent_duplicate_requests(sync_env):
    client = sync_env["client"]
    org_id = str(sync_env["org_a_id"])

    payload = {
        "connection_id": "conn-alpha-1",
        "job_type": "incremental_sync",
        "idempotency_key": "idemp-batch-2026-11-09",
    }

    # 1. First call creates job (201)
    res_1 = client.post(
        "/api/v1/sync-jobs",
        headers={"X-Organization-ID": org_id},
        json=payload,
    )
    assert res_1.status_code == 201
    job_1 = res_1.json()

    # 2. Second identical call returns existing job (200)
    res_2 = client.post(
        "/api/v1/sync-jobs",
        headers={"X-Organization-ID": org_id},
        json=payload,
    )
    assert res_2.status_code == 200
    job_2 = res_2.json()

    assert job_1["id"] == job_2["id"]
    assert job_1["idempotency_key"] == "idemp-batch-2026-11-09"

    # Verify only 1 audit event and 1 row created in database
    with sync_env["session_factory"]() as db:
        count = (
            db.query(SyncJob)
            .filter_by(
                organization_id=sync_env["org_a_id"],
                idempotency_key="idemp-batch-2026-11-09",
            )
            .count()
        )
        assert count == 1


def test_organization_scoped_listing_and_filtering(sync_env):
    client = sync_env["client"]
    org_id = str(sync_env["org_a_id"])

    # Create two jobs
    client.post(
        "/api/v1/sync-jobs",
        headers={"X-Organization-ID": org_id},
        json={"connection_id": "conn-alpha-1", "job_type": "job_1"},
    )
    client.post(
        "/api/v1/sync-jobs",
        headers={"X-Organization-ID": org_id},
        json={"connection_id": "conn-alpha-1", "job_type": "job_2"},
    )

    # List all
    res = client.get(
        "/api/v1/sync-jobs?limit=10&offset=0",
        headers={"X-Organization-ID": org_id},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2

    # Filter by status = queued
    res_queued = client.get(
        "/api/v1/sync-jobs?status=queued",
        headers={"X-Organization-ID": org_id},
    )
    assert res_queued.status_code == 200
    assert res_queued.json()["total"] == 2

    # Filter by status = running (none yet)
    res_running = client.get(
        "/api/v1/sync-jobs?status=running",
        headers={"X-Organization-ID": org_id},
    )
    assert res_running.status_code == 200
    assert res_running.json()["total"] == 0

    # Invalid status filter -> 400
    res_bad = client.get(
        "/api/v1/sync-jobs?status=invalid_status",
        headers={"X-Organization-ID": org_id},
    )
    assert res_bad.status_code == 400


def test_cross_tenant_job_access_fails_closed_with_404(sync_env):
    client = sync_env["client"]
    org_a = str(sync_env["org_a_id"])
    org_b = str(sync_env["org_b_id"])

    # Create a job in Org A
    create_res = client.post(
        "/api/v1/sync-jobs",
        headers={"X-Organization-ID": org_a},
        json={"connection_id": "conn-alpha-1"},
    )
    assert create_res.status_code == 201
    job_id = create_res.json()["id"]

    # Attempt to retrieve Org A's job with Org B context -> 404 (anti-enumeration)
    res_cross = client.get(
        f"/api/v1/sync-jobs/{job_id}",
        headers={"X-Organization-ID": org_b},
    )
    # Caller is not a member of Org B, so context returns 404
    assert res_cross.status_code == 404

    # Now make caller an owner in Org B to test resource isolation between member orgs
    with sync_env["session_factory"]() as db:
        db.add(
            OrganizationMembership(
                user_id=sync_env["owner_id"],
                organization_id=sync_env["org_b_id"],
                role="owner",
            )
        )
        db.commit()

    # Now caller has valid context in Org B, but job belongs to Org A -> 404
    res_cross_auth = client.get(
        f"/api/v1/sync-jobs/{job_id}",
        headers={"X-Organization-ID": org_b},
    )
    assert res_cross_auth.status_code == 404
    assert "Sync job not found" in res_cross_auth.json()["detail"]

    # Also cannot cancel Org A's job with Org B context -> 404
    res_cross_cancel = client.post(
        f"/api/v1/sync-jobs/{job_id}/cancel",
        headers={"X-Organization-ID": org_b},
    )
    assert res_cross_cancel.status_code == 404
    assert "Sync job not found" in res_cross_cancel.json()["detail"]

    # Also cannot create job referencing Org A connection in Org B
    res_bad_conn = client.post(
        "/api/v1/sync-jobs",
        headers={"X-Organization-ID": org_b},
        json={"connection_id": "conn-alpha-1"},
    )
    assert res_bad_conn.status_code == 404


def test_viewer_read_only_restrictions(sync_env):
    client = sync_env["client"]
    org_id = str(sync_env["org_a_id"])

    # 1. Owner creates a job
    create_res = client.post(
        "/api/v1/sync-jobs",
        headers={"X-Organization-ID": org_id},
        json={"connection_id": "conn-alpha-1"},
    )
    assert create_res.status_code == 201
    job_id = create_res.json()["id"]

    # 2. Switch principal to Viewer
    sync_env["principal_state"].update(
        {"user_id": sync_env["viewer_id"], "email": "viewer@orbit.test", "uid": "uid-sync-viewer"}
    )

    # Viewer CAN read jobs
    list_res = client.get("/api/v1/sync-jobs", headers={"X-Organization-ID": org_id})
    assert list_res.status_code == 200

    get_res = client.get(f"/api/v1/sync-jobs/{job_id}", headers={"X-Organization-ID": org_id})
    assert get_res.status_code == 200

    # Viewer CANNOT create jobs (403)
    v_create = client.post(
        "/api/v1/sync-jobs",
        headers={"X-Organization-ID": org_id},
        json={"connection_id": "conn-alpha-1"},
    )
    assert v_create.status_code == 403

    # Viewer CANNOT cancel jobs (403)
    v_cancel = client.post(
        f"/api/v1/sync-jobs/{job_id}/cancel",
        headers={"X-Organization-ID": org_id},
    )
    assert v_cancel.status_code == 403


def test_cancellation_lifecycle(sync_env):
    client = sync_env["client"]
    org_id = str(sync_env["org_a_id"])

    # Create job
    create_res = client.post(
        "/api/v1/sync-jobs",
        headers={"X-Organization-ID": org_id},
        json={"connection_id": "conn-alpha-1"},
    )
    job_id = create_res.json()["id"]

    # Cancel job
    cancel_res = client.post(
        f"/api/v1/sync-jobs/{job_id}/cancel",
        headers={"X-Organization-ID": org_id},
    )
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == "cancelled"
    assert cancel_res.json()["completed_at"] is not None

    # Verify audit event for cancellation
    with sync_env["session_factory"]() as db:
        audit = (
            db.query(AuditEvent)
            .filter_by(target_id=job_id, event_type="sync_job.cancelled")
            .first()
        )
        assert audit is not None
        assert audit.metadata_json["previous_status"] == "queued"

    # Attempt to cancel again (already terminal) -> 400
    cancel_again = client.post(
        f"/api/v1/sync-jobs/{job_id}/cancel",
        headers={"X-Organization-ID": org_id},
    )
    assert cancel_again.status_code == 400
    assert "terminal state 'cancelled'" in cancel_again.json()["detail"]


def test_worker_claim_and_successful_execution(sync_env):
    with sync_env["session_factory"]() as db:
        job = SyncJob(
            id=uuid4(),
            organization_id=sync_env["org_a_id"],
            connection_id="conn-alpha-1",
            requested_by_user_id=sync_env["owner_id"],
            job_type="incremental_sync",
            status="queued",
        )
        db.add(job)
        db.commit()
        job_id = job.id

    # Claim job
    with sync_env["session_factory"]() as db:
        claimed = claim_next_job(db, worker_id="worker-node-1", lease_duration_seconds=30)
        assert claimed is not None
        assert claimed.id == job_id
        assert claimed.status == "running"
        assert claimed.lease_token == "worker-node-1"
        assert claimed.attempt_count == 1
        assert claimed.started_at is not None

        # Execute through test adapter
        adapter = DeterministicTestAdapter(default_result={"synced_records": 100, "cursor": "2026-11-09"})
        processed = process_claimed_job(db, claimed, adapter, worker_id="worker-node-1")
        assert processed.status == "succeeded"
        assert processed.completed_at is not None
        assert processed.result_json == {"synced_records": 100, "cursor": "2026-11-09"}
        assert processed.lease_token is None

    # Verify sync_job.succeeded audit event
    with sync_env["session_factory"]() as db:
        audit = (
            db.query(AuditEvent)
            .filter_by(target_id=str(job_id), event_type="sync_job.succeeded")
            .first()
        )
        assert audit is not None
        assert audit.metadata_json["attempt_count"] == 1


def test_worker_retryable_failure_and_backoff(sync_env):
    with sync_env["session_factory"]() as db:
        job = SyncJob(
            id=uuid4(),
            organization_id=sync_env["org_a_id"],
            connection_id="conn-alpha-1",
            requested_by_user_id=sync_env["owner_id"],
            status="queued",
            max_attempts=3,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    # 1. First claim and retryable failure
    with sync_env["session_factory"]() as db:
        claimed = claim_next_job(db, worker_id="worker-1", job_id=job_id)
        assert claimed.attempt_count == 1

        adapter = DeterministicTestAdapter(
            behaviors={"conn-alpha-1": {"error": "rate limited by remote API", "retryable": True}}
        )
        processed = process_claimed_job(db, claimed, adapter, worker_id="worker-1")
        assert processed.status == "retrying"
        assert processed.next_retry_at is not None
        assert processed.error_json["message"] == "rate limited by remote API"
        assert processed.error_json["retryable"] is True

        # Ensure job is not claimed while next_retry_at is in the future
        unclaimed = claim_next_job(db, worker_id="worker-2")
        assert unclaimed is None

        # Fast-forward next_retry_at to past
        processed.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

        # 2. Second claim and permanent failure
        claimed_2 = claim_next_job(db, worker_id="worker-1", job_id=job_id)
        assert claimed_2.attempt_count == 2
        adapter_fatal = DeterministicTestAdapter(
            behaviors={"conn-alpha-1": {"error": "unauthorized bad credentials", "retryable": False}}
        )
        processed_2 = process_claimed_job(db, claimed_2, adapter_fatal, worker_id="worker-1")
        assert processed_2.status == "failed"
        assert processed_2.completed_at is not None
        assert processed_2.error_json["retryable"] is False


def test_worker_max_attempts_exceeded_becomes_failed(sync_env):
    with sync_env["session_factory"]() as db:
        job = SyncJob(
            id=uuid4(),
            organization_id=sync_env["org_a_id"],
            connection_id="conn-alpha-1",
            requested_by_user_id=sync_env["owner_id"],
            status="queued",
            attempt_count=2,  # Already at 2 of 3 attempts
            max_attempts=3,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    with sync_env["session_factory"]() as db:
        claimed = claim_next_job(db, worker_id="worker-1", job_id=job_id)
        assert claimed.attempt_count == 3  # Now at max

        adapter = DeterministicTestAdapter(
            behaviors={"conn-alpha-1": {"error": "temporary timeout", "retryable": True}}
        )
        processed = process_claimed_job(db, claimed, adapter, worker_id="worker-1")
        assert processed.status == "failed"
        assert "Maximum attempts (3) reached" in processed.error_json["message"]


def test_worker_connection_mismatch_fails_safely(sync_env):
    # Job claims to be for Org B's connection while residing in Org A
    with sync_env["session_factory"]() as db:
        mismatched_job = SyncJob(
            id=uuid4(),
            organization_id=sync_env["org_a_id"],
            connection_id="conn-beta-1",  # belongs to Org B!
            requested_by_user_id=sync_env["owner_id"],
            status="queued",
        )
        db.add(mismatched_job)
        db.commit()
        job_id = mismatched_job.id

    with sync_env["session_factory"]() as db:
        claimed = claim_next_job(db, worker_id="worker-1", job_id=job_id)
        adapter = DeterministicTestAdapter()
        processed = process_claimed_job(db, claimed, adapter, worker_id="worker-1")

        assert processed.status == "failed"
        assert "does not belong to the job organization" in processed.error_json["message"]
        # Ensure adapter was never called on foreign connection
        assert len(adapter.call_log) == 0


def test_worker_lease_recovery_after_crash(sync_env):
    # Simulate a job that was claimed by a crashed worker whose lease expired
    with sync_env["session_factory"]() as db:
        crashed_job = SyncJob(
            id=uuid4(),
            organization_id=sync_env["org_a_id"],
            connection_id="conn-alpha-1",
            status="running",
            attempt_count=1,
            lease_token="crashed-worker-node",
            lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),  # expired
        )
        db.add(crashed_job)
        db.commit()
        job_id = crashed_job.id

    # Another worker should recover and claim this job
    with sync_env["session_factory"]() as db:
        recovered = claim_next_job(db, worker_id="survivor-worker-node", job_id=job_id)
        assert recovered is not None
        assert recovered.id == job_id
        assert recovered.status == "running"
        assert recovered.lease_token == "survivor-worker-node"
        assert recovered.attempt_count == 2


def test_secret_redaction_in_worker_result(sync_env):
    with sync_env["session_factory"]() as db:
        job = SyncJob(
            id=uuid4(),
            organization_id=sync_env["org_a_id"],
            connection_id="conn-alpha-1",
            status="queued",
        )
        db.add(job)
        db.commit()
        job_id = job.id

    with sync_env["session_factory"]() as db:
        claimed = claim_next_job(db, worker_id="worker-redaction-test", job_id=job_id)

        # Adapter returns payload with sensitive fields accidentally included
        leaky_payload = {
            "synced_rows": 50,
            "access_token": "secret_oauth_token_123",
            "api_key": "private_api_key_456",
            "password": "hidden_password",
            "nested": {
                "safe": "public_data",
                "refresh_token": "sensitive_refresh_token",
            },
        }
        adapter = DeterministicTestAdapter(default_result=leaky_payload)
        processed = process_claimed_job(db, claimed, adapter, worker_id="worker-redaction-test")

        res = processed.result_json
        assert res["synced_rows"] == 50
        assert "access_token" not in res
        assert "api_key" not in res
        assert "password" not in res
        assert res["nested"]["safe"] == "public_data"
        assert "refresh_token" not in res["nested"]


def test_audit_failure_rolls_back_job_creation(sync_env):
    client = sync_env["client"]
    org_id = str(sync_env["org_a_id"])

    # Install trigger to abort audit insertion for sync_job.created
    with sync_env["engine"].begin() as conn:
        conn.execute(
            text(
                "CREATE TRIGGER abort_sync_job_audit "
                "BEFORE INSERT ON audit_events "
                "WHEN NEW.event_type = 'sync_job.created' "
                "BEGIN SELECT RAISE(ABORT, 'sync job audit aborted'); END;"
            )
        )

    res = client.post(
        "/api/v1/sync-jobs",
        headers={"X-Organization-ID": org_id},
        json={"connection_id": "conn-alpha-1", "job_type": "aborted_job"},
    )
    assert res.status_code == 500

    # Ensure SyncJob was rolled back and does not exist in DB
    with sync_env["session_factory"]() as db:
        row = (
            db.query(SyncJob)
            .filter_by(job_type="aborted_job")
            .one_or_none()
        )
        assert row is None


def test_concurrent_worker_claim_mutual_exclusion(sync_env):
    with sync_env["session_factory"]() as db:
        job = SyncJob(
            id=uuid4(),
            organization_id=sync_env["org_a_id"],
            connection_id="conn-alpha-1",
            status="queued",
        )
        db.add(job)
        db.commit()
        job_id = job.id

    # Worker 1 claims the job
    with sync_env["session_factory"]() as db1:
        worker1_claim = claim_next_job(db1, worker_id="worker-node-1", lease_duration_seconds=60)
        assert worker1_claim is not None
        assert worker1_claim.id == job_id
        assert worker1_claim.lease_token == "worker-node-1"

    # Worker 2 attempts to claim while Worker 1 holds active lease
    with sync_env["session_factory"]() as db2:
        worker2_claim = claim_next_job(db2, worker_id="worker-node-2", lease_duration_seconds=60)
        assert worker2_claim is None


def test_terminal_jobs_cannot_be_claimed_or_restarted(sync_env):
    with sync_env["session_factory"]() as db:
        now = datetime.now(timezone.utc)
        j_succ = SyncJob(
            id=uuid4(),
            organization_id=sync_env["org_a_id"],
            connection_id="conn-alpha-1",
            status="succeeded",
            completed_at=now,
        )
        j_fail = SyncJob(
            id=uuid4(),
            organization_id=sync_env["org_a_id"],
            connection_id="conn-alpha-1",
            status="failed",
            completed_at=now,
        )
        j_canc = SyncJob(
            id=uuid4(),
            organization_id=sync_env["org_a_id"],
            connection_id="conn-alpha-1",
            status="cancelled",
            completed_at=now,
        )
        db.add_all([j_succ, j_fail, j_canc])
        db.commit()
        succ_id, fail_id, canc_id = j_succ.id, j_fail.id, j_canc.id

    with sync_env["session_factory"]() as db:
        # None of the terminal jobs can be claimed by a worker
        assert claim_next_job(db, worker_id="worker-1", job_id=succ_id) is None
        assert claim_next_job(db, worker_id="worker-1", job_id=fail_id) is None
        assert claim_next_job(db, worker_id="worker-1", job_id=canc_id) is None
        assert claim_next_job(db, worker_id="worker-1") is None

    # None of the terminal jobs can be cancelled again via API
    client = sync_env["client"]
    org_id = str(sync_env["org_a_id"])
    for jid in (succ_id, fail_id, canc_id):
        res = client.post(f"/api/v1/sync-jobs/{jid}/cancel", headers={"X-Organization-ID": org_id})
        assert res.status_code == 400
        assert "terminal state" in res.json()["detail"]


def test_worker_cannot_overwrite_cancelled_job_with_success(sync_env):
    from app.workers.local_worker import SyncWorkerError

    client = sync_env["client"]
    org_id = str(sync_env["org_a_id"])

    # 1. Create a job
    create_res = client.post(
        "/api/v1/sync-jobs",
        headers={"X-Organization-ID": org_id},
        json={"connection_id": "conn-alpha-1"},
    )
    job_id = UUID(create_res.json()["id"])

    # 2. Worker claims the job
    with sync_env["session_factory"]() as db_worker:
        claimed = claim_next_job(db_worker, worker_id="slow-worker", job_id=job_id)
        assert claimed is not None
        assert claimed.status == "running"
        assert claimed.lease_token == "slow-worker"

        # 3. User cancels the job while worker is "executing"
        cancel_res = client.post(
            f"/api/v1/sync-jobs/{job_id}/cancel",
            headers={"X-Organization-ID": org_id},
        )
        assert cancel_res.status_code == 200
        assert cancel_res.json()["status"] == "cancelled"

        # 4. Slow worker finishes adapter execution and attempts to transition to succeeded
        adapter = DeterministicTestAdapter(default_result={"late_data": 123})
        with pytest.raises(SyncWorkerError) as exc_info:
            process_claimed_job(db_worker, claimed, adapter, worker_id="slow-worker")
        assert "lost active lease" in str(exc_info.value)

    # 5. Verify database state: status remains "cancelled" and no succeeded audit event exists
    with sync_env["session_factory"]() as db:
        job = db.query(SyncJob).filter_by(id=job_id).one()
        assert job.status == "cancelled"
        assert job.result_json == {}

        succ_audit = (
            db.query(AuditEvent)
            .filter_by(target_id=str(job_id), event_type="sync_job.succeeded")
            .first()
        )
        assert succ_audit is None


def test_worker_cannot_overwrite_expired_lease_claimed_by_another_worker(sync_env):
    from app.workers.local_worker import SyncWorkerError

    # 1. Create running job with worker A whose lease expired
    with sync_env["session_factory"]() as db:
        job = SyncJob(
            id=uuid4(),
            organization_id=sync_env["org_a_id"],
            connection_id="conn-alpha-1",
            status="running",
            attempt_count=1,
            lease_token="worker-A",
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        db.add(job)
        db.commit()
        job_id = job.id

    # 2. Worker B recovers the job
    with sync_env["session_factory"]() as db_worker_b:
        recovered = claim_next_job(db_worker_b, worker_id="worker-B", job_id=job_id)
        assert recovered.status == "running"
        assert recovered.lease_token == "worker-B"
        assert recovered.attempt_count == 2

    # 3. Worker A wakes up late and attempts to process
    with sync_env["session_factory"]() as db_worker_a:
        stale_job = db_worker_a.query(SyncJob).filter_by(id=job_id).first()
        stale_job.lease_token = "worker-A"  # in-memory stale state
        adapter = DeterministicTestAdapter()
        with pytest.raises(SyncWorkerError) as exc_info:
            process_claimed_job(db_worker_a, stale_job, adapter, worker_id="worker-A")
        assert "lost active lease" in str(exc_info.value)

    # 4. Verify in DB that Worker B's lease was not overwritten
    with sync_env["session_factory"]() as db:
        final_job = db.query(SyncJob).filter_by(id=job_id).one()
        assert final_job.status == "running"
        assert final_job.lease_token == "worker-B"
        assert final_job.attempt_count == 2


def test_audit_failure_rolls_back_job_cancellation(sync_env):
    client = sync_env["client"]
    org_id = str(sync_env["org_a_id"])

    # Create job
    create_res = client.post(
        "/api/v1/sync-jobs",
        headers={"X-Organization-ID": org_id},
        json={"connection_id": "conn-alpha-1"},
    )
    job_id = create_res.json()["id"]

    # Install trigger to abort audit insertion for sync_job.cancelled
    with sync_env["engine"].begin() as conn:
        conn.execute(
            text(
                "CREATE TRIGGER abort_sync_job_cancel_audit "
                "BEFORE INSERT ON audit_events "
                "WHEN NEW.event_type = 'sync_job.cancelled' "
                "BEGIN SELECT RAISE(ABORT, 'cancel audit aborted'); END;"
            )
        )

    # Attempt to cancel job -> should fail with 500
    cancel_res = client.post(
        f"/api/v1/sync-jobs/{job_id}/cancel",
        headers={"X-Organization-ID": org_id},
    )
    assert cancel_res.status_code == 500

    # Ensure job status remains 'queued' and was rolled back
    with sync_env["session_factory"]() as db:
        job = db.query(SyncJob).filter_by(id=UUID(job_id)).one()
        assert job.status == "queued"
        assert job.completed_at is None


def test_error_message_and_audit_redaction_of_bearer_tokens_and_credentials(sync_env):
    with sync_env["session_factory"]() as db:
        job = SyncJob(
            id=uuid4(),
            organization_id=sync_env["org_a_id"],
            connection_id="conn-alpha-1",
            requested_by_user_id=sync_env["owner_id"],
            status="queued",
        )
        db.add(job)
        db.commit()
        job_id = job.id

    with sync_env["session_factory"]() as db:
        claimed = claim_next_job(db, worker_id="worker-redact", job_id=job_id)

        # Fatal error that contains sensitive tokens in error message
        leaky_error_message = (
            "HTTP 401 Unauthorized for Bearer ya29.a0AfH6SMA-super-secret-token-xyz "
            "with api_key=top_secret_prod_key_999 and refresh_token=secret_refresh_abc"
        )
        adapter = DeterministicTestAdapter(
            behaviors={"conn-alpha-1": {"error": leaky_error_message, "retryable": False}}
        )
        processed = process_claimed_job(db, claimed, adapter, worker_id="worker-redact")

        assert processed.status == "failed"
        msg = processed.error_json["message"]
        assert "Bearer [REDACTED]" in msg
        assert "api_key=[REDACTED]" in msg
        assert "refresh_token=[REDACTED]" in msg
        assert "ya29.a0AfH6SMA" not in msg
        assert "top_secret_prod_key_999" not in msg
        assert "secret_refresh_abc" not in msg

    # Verify audit event error_message was also redacted
    with sync_env["session_factory"]() as db:
        audit = (
            db.query(AuditEvent)
            .filter_by(target_id=str(job_id), event_type="sync_job.failed")
            .first()
        )
        assert audit is not None
        audit_msg = audit.metadata_json["error_message"]
        assert "Bearer [REDACTED]" in audit_msg
        assert "ya29.a0AfH6SMA" not in audit_msg
        assert "top_secret_prod_key_999" not in audit_msg


