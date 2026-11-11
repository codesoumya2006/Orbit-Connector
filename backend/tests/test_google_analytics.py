from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base, get_db
from app.core.firebase_auth import AuthenticatedPrincipal, get_current_principal
from app.core.security import encrypt_secret
from app.main import app
from app.models.entities import (
    AuditEvent,
    GA4ReportRow,
    IntegrationConnection,
    OAuthState,
    Organization,
    OrganizationMembership,
    SyncJob,
    User,
)
from app.providers.google_analytics.adapter import GoogleAnalyticsAdapter
from app.providers.google_analytics.admin import list_accounts_and_properties, verify_property_accessible
from app.providers.google_analytics.oauth import (
    build_google_authorization_url,
    exchange_authorization_code,
    generate_pkce,
    refresh_access_token,
)
from app.providers.google_analytics.reports import fetch_ga4_report, validate_date_range
from app.workers.local_worker import (
    PermanentSyncError,
    RetryableSyncError,
    SyncWorkerError,
    _verify_active_lease,
    claim_next_job,
    process_claimed_job,
    run_worker_loop,
)


@pytest.fixture
def ga_env(monkeypatch):
    monkeypatch.setattr(settings, "credential_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "google_client_id", "test-google-client-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_client_secret", "test-google-client-secret")
    monkeypatch.setattr(settings, "google_redirect_uri", "http://localhost:8000/api/v1/oauth/google/callback")

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        owner = User(
            id=uuid4(),
            firebase_uid="uid-ga-owner",
            email="owner@orbit.test",
            email_verified=True,
        )
        viewer = User(
            id=uuid4(),
            firebase_uid="uid-ga-viewer",
            email="viewer@orbit.test",
            email_verified=True,
        )
        db.add_all([owner, viewer])
        db.flush()

        org_a = Organization(id=uuid4(), name="GA Org A")
        org_b = Organization(id=uuid4(), name="GA Org B")
        db.add_all([org_a, org_b])
        db.flush()

        db.add(OrganizationMembership(user_id=owner.id, organization_id=org_a.id, role="owner"))
        db.add(OrganizationMembership(user_id=viewer.id, organization_id=org_a.id, role="viewer"))

        conn_a = IntegrationConnection(
            id="conn-ga-alpha",
            organization_id=org_a.id,
            provider_key="google_analytics",
            display_name="GA4 Alpha",
            status="needs_authorization",
            connection_type="oauth",
            config_json={},
        )
        conn_b = IntegrationConnection(
            id="conn-ga-beta",
            organization_id=org_b.id,
            provider_key="google_analytics",
            display_name="GA4 Beta",
            status="needs_authorization",
            connection_type="oauth",
            config_json={},
        )
        db.add_all([conn_a, conn_b])
        db.commit()

        org_a_id = org_a.id
        org_b_id = org_b.id
        owner_id = owner.id
        viewer_id = viewer.id

    principal_state = {
        "user_id": owner_id,
        "email": "owner@orbit.test",
        "uid": "uid-ga-owner",
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

    with TestClient(app, raise_server_exceptions=True) as client:
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


def test_pkce_and_oauth_authorization_url_generation(ga_env):
    client = ga_env["client"]
    org_id = str(ga_env["org_a_id"])

    # 1. Generate PKCE verifier and challenge
    verifier, challenge = generate_pkce()
    assert len(verifier) >= 43
    assert len(challenge) >= 43

    # 2. Initiate OAuth endpoint
    res = client.post(
        "/api/v1/oauth/google/authorize?connection_id=conn-ga-alpha",
        headers={"X-Organization-ID": org_id},
    )
    assert res.status_code == 200
    data = res.json()
    assert "authorization_url" in data
    assert "state" in data
    assert data["connection_id"] == "conn-ga-alpha"
    assert "accounts.google.com/o/oauth2/v2/auth" in data["authorization_url"]
    assert "analytics.readonly" in data["authorization_url"]
    assert "code_challenge=" in data["authorization_url"]

    # Verify state row in DB
    with ga_env["session_factory"]() as db:
        state_row = db.query(OAuthState).filter_by(state_token=data["state"]).first()
        assert state_row is not None
        assert state_row.connection_id == "conn-ga-alpha"
        assert state_row.used_at is None
        # Verify PKCE verifier is encrypted at rest (not plaintext)
        assert state_row.code_verifier.startswith("gAAAAA")
        from app.core.security import decrypt_secret
        decrypted = decrypt_secret(state_row.code_verifier)
        assert decrypted is not None
        assert len(decrypted) >= 43
        exp = state_row.expires_at.replace(tzinfo=timezone.utc) if state_row.expires_at.tzinfo is None else state_row.expires_at
        assert exp > datetime.now(timezone.utc)


def test_oauth_state_validation_expiration_and_replay_rejection(ga_env, monkeypatch):
    client = ga_env["client"]
    org_id = str(ga_env["org_a_id"])

    # 1. Invalid/unknown state
    res_unknown = client.get(
        "/api/v1/oauth/google/callback?code=test-code&state=nonexistent-state-token",
        headers={"Accept": "application/json"},
    )
    assert res_unknown.status_code == 400
    assert "Invalid or unrecognized OAuth state token" in res_unknown.json()["detail"]

    # 2. Expired state
    with ga_env["session_factory"]() as db:
        expired_state = OAuthState(
            state_token="expired-state-token",
            code_verifier="test-verifier",
            organization_id=ga_env["org_a_id"],
            user_id=ga_env["owner_id"],
            connection_id="conn-ga-alpha",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),  # in past
        )
        db.add(expired_state)
        db.commit()

    res_expired = client.get(
        "/api/v1/oauth/google/callback?code=test-code&state=expired-state-token",
        headers={"Accept": "application/json"},
    )
    assert res_expired.status_code == 400
    assert "OAuth state token has expired" in res_expired.json()["detail"]

    # 3. Valid state consumed, then replayed (single-use enforcement)
    with ga_env["session_factory"]() as db:
        valid_state = OAuthState(
            state_token="valid-state-token-123",
            code_verifier="test-verifier-xyz",
            organization_id=ga_env["org_a_id"],
            user_id=ga_env["owner_id"],
            connection_id="conn-ga-alpha",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        db.add(valid_state)
        db.commit()

    # Mock successful token exchange
    def mock_exchange(code, code_verifier, client=None):
        return {
            "access_token": "ya29.mock_access_token_123",
            "refresh_token": "1//mock_refresh_token_456",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/analytics.readonly",
        }

    monkeypatch.setattr(
        "app.api.google_analytics.exchange_authorization_code",
        mock_exchange,
    )

    # First call succeeds
    res_success = client.get(
        "/api/v1/oauth/google/callback?code=test-auth-code&state=valid-state-token-123",
        headers={"Accept": "application/json"},
    )
    assert res_success.status_code == 200
    assert res_success.json()["connection_status"] == "needs_configuration"

    # Second call with same state fails with replay rejection
    res_replay = client.get(
        "/api/v1/oauth/google/callback?code=test-auth-code&state=valid-state-token-123",
        headers={"Accept": "application/json"},
    )
    assert res_replay.status_code == 400
    assert "already been consumed" in res_replay.json()["detail"]


def test_oauth_error_and_missing_refresh_token(ga_env, monkeypatch):
    client = ga_env["client"]

    # 1. Error passed by Google (e.g. user denied consent)
    res_err = client.get(
        "/api/v1/oauth/google/callback?error=access_denied&state=some-state",
        headers={"Accept": "application/json"},
    )
    assert res_err.status_code == 400
    assert "access_denied" in res_err.json()["detail"]

    # 2. Missing refresh token on new connection
    with ga_env["session_factory"]() as db:
        st = OAuthState(
            state_token="state-no-rt",
            code_verifier="test-verif",
            organization_id=ga_env["org_a_id"],
            user_id=ga_env["owner_id"],
            connection_id="conn-ga-alpha",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        db.add(st)
        db.commit()

    def mock_exchange_no_rt(code, code_verifier, client=None):
        return {
            "access_token": "ya29.mock_access_token",
            "expires_in": 3600,
            # no refresh_token!
        }

    monkeypatch.setattr(
        "app.api.google_analytics.exchange_authorization_code",
        mock_exchange_no_rt,
    )

    res_no_rt = client.get(
        "/api/v1/oauth/google/callback?code=auth-code&state=state-no-rt",
        headers={"Accept": "application/json"},
    )
    assert res_no_rt.status_code == 400
    assert "did not return a refresh token" in res_no_rt.json()["detail"]


def test_encrypted_credential_persistence_and_redaction(ga_env, monkeypatch):
    client = ga_env["client"]
    org_id = str(ga_env["org_a_id"])

    # Setup valid state
    with ga_env["session_factory"]() as db:
        st = OAuthState(
            state_token="state-encrypt-test",
            code_verifier="verif-123",
            organization_id=ga_env["org_a_id"],
            user_id=ga_env["owner_id"],
            connection_id="conn-ga-alpha",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        db.add(st)
        db.commit()

    raw_secret_refresh_token = "1//super_secret_google_refresh_token_98765"

    def mock_exchange(code, code_verifier, client=None):
        return {
            "access_token": "ya29.secret_access_token",
            "refresh_token": raw_secret_refresh_token,
            "expires_in": 3600,
        }

    monkeypatch.setattr(
        "app.api.google_analytics.exchange_authorization_code",
        mock_exchange,
    )

    res = client.get(
        "/api/v1/oauth/google/callback?code=auth-code&state=state-encrypt-test",
        headers={"Accept": "application/json"},
    )
    assert res.status_code == 200

    # Inspect connection in DB
    with ga_env["session_factory"]() as db:
        conn = db.query(IntegrationConnection).filter_by(id="conn-ga-alpha").one()
        assert conn.status == "needs_configuration"
        encrypted_val = conn.config_json["refresh_token_encrypted"]
        # Must be encrypted Fernet string, not raw plaintext
        assert encrypted_val != raw_secret_refresh_token
        assert raw_secret_refresh_token not in str(conn.config_json)

        # Inspect audit event
        audit = (
            db.query(AuditEvent)
            .filter_by(target_id="conn-ga-alpha", event_type="connection.oauth_authorized")
            .first()
        )
        assert audit is not None
        assert raw_secret_refresh_token not in str(audit.metadata_json)
        assert "refresh_token" not in str(audit.metadata_json)

    # Inspect GET /connections response
    get_res = client.get(
        "/api/v1/connections",
        headers={"X-Organization-ID": org_id},
    )
    assert get_res.status_code == 200
    conn_items = [c for c in get_res.json() if c["id"] == "conn-ga-alpha"]
    assert len(conn_items) == 1
    item = conn_items[0]
    assert item["has_secret"] is True
    # Ensure raw or encrypted refresh token is NEVER exposed to API response
    assert "refresh_token" not in item
    assert "refresh_token_encrypted" not in item
    assert raw_secret_refresh_token not in str(item)


def test_property_discovery_and_selection(ga_env, monkeypatch):
    client = ga_env["client"]
    org_id = str(ga_env["org_a_id"])

    # Seed authorized connection with encrypted refresh token
    with ga_env["session_factory"]() as db:
        conn = db.query(IntegrationConnection).filter_by(id="conn-ga-alpha").one()
        conn.status = "needs_configuration"
        conn.config_json = {
            "refresh_token_encrypted": encrypt_secret("1//test_refresh_token"),
            "authorized_at": datetime.now(timezone.utc).isoformat(),
        }
        db.commit()

    # Mock refresh token call
    monkeypatch.setattr(
        "app.api.google_analytics.refresh_access_token",
        lambda rt, client=None: "ya29.mocked_access_token",
    )

    # Mock Admin API discovery
    mock_properties = [
        {
            "property_id": "properties/123456789",
            "display_name": "Production Store GA4",
            "account_id": "accounts/111",
            "account_name": "Main Account",
        },
        {
            "property_id": "properties/987654321",
            "display_name": "Blog GA4",
            "account_id": "accounts/111",
            "account_name": "Main Account",
        },
    ]
    monkeypatch.setattr(
        "app.providers.google_analytics.admin.list_accounts_and_properties",
        lambda access_token=None, client=None, **kwargs: mock_properties,
    )
    monkeypatch.setattr(
        "app.api.google_analytics.list_accounts_and_properties",
        lambda access_token=None, client=None, **kwargs: mock_properties,
    )

    # 1. Discover properties
    props_res = client.get(
        "/api/v1/connections/conn-ga-alpha/google-analytics/properties",
        headers={"X-Organization-ID": org_id},
    )
    assert props_res.status_code == 200
    props_data = props_res.json()
    assert len(props_data) == 2
    assert props_data[0]["property_id"] == "properties/123456789"
    assert props_data[0]["display_name"] == "Production Store GA4"

    # 2. Select invalid/inaccessible property -> rejected with 400
    bad_select = client.post(
        "/api/v1/connections/conn-ga-alpha/google-analytics/properties/select",
        headers={"X-Organization-ID": org_id},
        json={"property_id": "properties/999999999", "display_name": "Fake Property"},
    )
    assert bad_select.status_code == 400
    assert "not accessible to the connected Google account" in bad_select.json()["detail"]

    # 3. Select valid property -> succeeds
    good_select = client.post(
        "/api/v1/connections/conn-ga-alpha/google-analytics/properties/select",
        headers={"X-Organization-ID": org_id},
        json={
            "property_id": "properties/123456789",
            "display_name": "Production Store GA4",
            "account_id": "accounts/111",
            "account_name": "Main Account",
        },
    )
    assert good_select.status_code == 200
    assert good_select.json()["status"] == "connected"
    assert good_select.json()["property_id"] == "properties/123456789"

    # Verify state in DB
    with ga_env["session_factory"]() as db:
        conn_after = db.query(IntegrationConnection).filter_by(id="conn-ga-alpha").one()
        assert conn_after.status == "connected"
        assert conn_after.config_json["property_id"] == "properties/123456789"
        assert conn_after.config_json["property_name"] == "Production Store GA4"

        # Verify audit event
        audit = (
            db.query(AuditEvent)
            .filter_by(target_id="conn-ga-alpha", event_type="connection.property_selected")
            .first()
        )
        assert audit is not None


def test_cross_tenant_and_viewer_restrictions(ga_env, monkeypatch):
    client = ga_env["client"]
    org_a = str(ga_env["org_a_id"])
    org_b = str(ga_env["org_b_id"])

    # 1. Cross-tenant access to conn-ga-alpha with Org B context returns 404
    cross_res = client.get(
        "/api/v1/connections/conn-ga-alpha/google-analytics/properties",
        headers={"X-Organization-ID": org_b},
    )
    assert cross_res.status_code == 404

    # 2. Viewer restrictions
    ga_env["principal_state"].update(
        {"user_id": ga_env["viewer_id"], "email": "viewer@orbit.test", "uid": "uid-ga-viewer"}
    )

    # Viewer CANNOT initiate OAuth (403)
    v_auth = client.post(
        "/api/v1/oauth/google/authorize?connection_id=conn-ga-alpha",
        headers={"X-Organization-ID": org_a},
    )
    assert v_auth.status_code == 403

    # Viewer CANNOT select property (403)
    v_select = client.post(
        "/api/v1/connections/conn-ga-alpha/google-analytics/properties/select",
        headers={"X-Organization-ID": org_a},
        json={"property_id": "properties/12345"},
    )
    assert v_select.status_code == 403

    # Viewer CANNOT disconnect (403)
    v_disconn = client.post(
        "/api/v1/connections/conn-ga-alpha/disconnect",
        headers={"X-Organization-ID": org_a},
    )
    assert v_disconn.status_code == 403


def test_ga4_report_normalization_and_date_validation():
    # 1. Date validation
    validate_date_range("2026-10-01", "2026-10-31")
    validate_date_range("30daysAgo", "yesterday")

    with pytest.raises(ValueError, match="cannot be after end_date"):
        validate_date_range("2026-11-01", "2026-10-01")

    with pytest.raises(ValueError, match="Invalid start_date"):
        validate_date_range("invalid-date", "2026-10-01")

    # 2. Report normalization with mock response
    class MockDataResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {
                "rows": [
                    {
                        "dimensionValues": [{"value": "20261101"}, {"value": "Organic Search"}],
                        "metricValues": [{"value": "150"}, {"value": "120"}, {"value": "450"}],
                    },
                    {
                        "dimensionValues": [{"value": "20261102"}, {"value": "Direct"}],
                        "metricValues": [{"value": "80"}, {"value": "65"}, {"value": "210"}],
                    },
                ]
            }

    class MockClient:
        def post(self, url, headers=None, json=None):
            return MockDataResponse()

    rows = fetch_ga4_report(
        access_token="test_tok",
        property_id="properties/12345",
        start_date="2026-11-01",
        end_date="2026-11-02",
        client=MockClient(),
    )
    assert len(rows) == 2
    assert rows[0].report_date == "2026-11-01"
    assert rows[0].channel_group == "Organic Search"
    assert rows[0].sessions == 150
    assert rows[0].active_users == 120
    assert rows[0].event_count == 450


def test_ga4_error_classification():
    # 401 permanent error
    class Mock401Response:
        status_code = 401
        headers = {"content-type": "application/json"}

        def json(self):
            return {"error": {"message": "Invalid credentials", "status": "UNAUTHENTICATED"}}

    class MockClient401:
        def post(self, url, headers=None, json=None):
            return Mock401Response()

    with pytest.raises(PermanentSyncError):
        fetch_ga4_report(access_token="tok", property_id="12345", client=MockClient401())

    # 429 quota retryable error
    class Mock429Response:
        status_code = 429
        headers = {"content-type": "application/json"}

        def json(self):
            return {"error": {"message": "Rate limit exceeded", "status": "RESOURCE_EXHAUSTED"}}

    class MockClient429:
        def post(self, url, headers=None, json=None):
            return Mock429Response()

    with pytest.raises(RetryableSyncError):
        fetch_ga4_report(access_token="tok", property_id="12345", client=MockClient429())


def test_syncjob_worker_ga4_ingestion_and_safe_upsert(ga_env, monkeypatch):
    client = ga_env["client"]
    org_id = str(ga_env["org_a_id"])

    # 1. Seed connected GA4 connection with encrypted credentials and property
    with ga_env["session_factory"]() as db:
        conn = db.query(IntegrationConnection).filter_by(id="conn-ga-alpha").one()
        conn.status = "connected"
        conn.config_json = {
            "refresh_token_encrypted": encrypt_secret("1//test_rt_for_worker"),
            "property_id": "properties/123456789",
            "property_name": "Store GA4",
        }
        db.commit()

    # 2. Mock token refresh and Data API report
    monkeypatch.setattr(
        "app.providers.google_analytics.adapter.refresh_access_token",
        lambda rt, client=None: "ya29.refreshed_worker_tok",
    )

    from app.providers.google_analytics.reports import NormalizedReportRow

    sample_rows = [
        NormalizedReportRow(
            report_date="2026-11-01",
            channel_group="Organic Search",
            sessions=100,
            active_users=80,
            event_count=300,
        ),
        NormalizedReportRow(
            report_date="2026-11-01",
            channel_group="Paid Search",
            sessions=50,
            active_users=45,
            event_count=120,
        ),
    ]

    monkeypatch.setattr(
        "app.providers.google_analytics.adapter.fetch_ga4_report",
        lambda access_token, property_id, start_date, end_date, client=None: sample_rows,
    )

    # 3. Create a GA4 sync job via API
    create_job_res = client.post(
        "/api/v1/sync-jobs",
        headers={"X-Organization-ID": org_id},
        json={
            "connection_id": "conn-ga-alpha",
            "job_type": "ga4_sync",
            "metadata": {"start_date": "2026-11-01", "end_date": "2026-11-01"},
        },
    )
    assert create_job_res.status_code == 201
    job_id = UUID(create_job_res.json()["id"])

    # 4. Worker claims and executes job
    with ga_env["session_factory"]() as db:
        claimed = claim_next_job(db, worker_id="ga4-worker-1", job_id=job_id)
        assert claimed is not None
        assert claimed.status == "running"

        # Adapter resolved automatically via connection.provider_key == "google_analytics"
        processed = process_claimed_job(db, claimed, adapter=None, worker_id="ga4-worker-1")
        assert processed.status == "succeeded"
        assert processed.result_json["rows_synced"] == 2
        assert processed.result_json["total_sessions"] == 150
        assert processed.result_json["total_active_users"] == 125
        assert processed.result_json["total_event_count"] == 420

    # 5. Verify records stored in ga4_report_rows
    with ga_env["session_factory"]() as db:
        rows = (
            db.query(GA4ReportRow)
            .filter_by(organization_id=ga_env["org_a_id"], connection_id="conn-ga-alpha")
            .all()
        )
        assert len(rows) == 2

    # 6. Test safe upsert: execute sync again with updated metrics -> no duplicate rows created
    updated_rows = [
        NormalizedReportRow(
            report_date="2026-11-01",
            channel_group="Organic Search",
            sessions=130,  # updated
            active_users=95,
            event_count=350,
        ),
        NormalizedReportRow(
            report_date="2026-11-01",
            channel_group="Paid Search",
            sessions=50,
            active_users=45,
            event_count=120,
        ),
    ]
    monkeypatch.setattr(
        "app.providers.google_analytics.adapter.fetch_ga4_report",
        lambda access_token, property_id, start_date, end_date, client=None: updated_rows,
    )

    # Create second job
    job2_res = client.post(
        "/api/v1/sync-jobs",
        headers={"X-Organization-ID": org_id},
        json={"connection_id": "conn-ga-alpha", "job_type": "ga4_sync"},
    )
    job2_id = UUID(job2_res.json()["id"])

    with ga_env["session_factory"]() as db:
        claimed_2 = claim_next_job(db, worker_id="ga4-worker-1", job_id=job2_id)
        processed_2 = process_claimed_job(db, claimed_2, adapter=None, worker_id="ga4-worker-1")
        assert processed_2.status == "succeeded"

        # Still exactly 2 rows (upserted, not duplicated!)
        final_rows = (
            db.query(GA4ReportRow)
            .filter_by(organization_id=ga_env["org_a_id"], connection_id="conn-ga-alpha")
            .all()
        )
        assert len(final_rows) == 2
        organic_row = [r for r in final_rows if r.channel_group == "Organic Search"][0]
        assert organic_row.sessions == 130  # updated in place

    # 7. Retrieve reports via API
    rep_res = client.get(
        "/api/v1/connections/conn-ga-alpha/google-analytics/reports",
        headers={"X-Organization-ID": org_id},
    )
    assert rep_res.status_code == 200
    rep_data = rep_res.json()
    assert rep_data["total"] == 2
    assert len(rep_data["items"]) == 2


def test_lease_expiry_prevents_stale_overwrite(ga_env):
    """Verify that an expired worker lease cannot succeed a job or overwrite internal state."""
    with ga_env["session_factory"]() as db:
        now = datetime.now(timezone.utc)
        # Create a job that is running with an already-expired lease
        stale_job = SyncJob(
            id=uuid4(),
            organization_id=ga_env["org_a_id"],
            connection_id="conn-ga-alpha",
            job_type="ga4_sync",
            status="running",
            attempt_count=1,
            max_attempts=3,
            lease_token="stale-worker",
            lease_expires_at=now - timedelta(seconds=10),  # expired!
            created_at=now - timedelta(minutes=5),
            started_at=now - timedelta(minutes=1),
            updated_at=now - timedelta(minutes=1),
        )
        db.add(stale_job)
        db.commit()
        job_id = stale_job.id

        # Attempting to verify active lease for stale-worker must fail and raise SyncWorkerError
        with pytest.raises(SyncWorkerError) as exc_info:
            _verify_active_lease(db, job_id=job_id, worker_id="stale-worker")
        assert "lease expired" in str(exc_info.value).lower()


def test_reauthorization_preserves_existing_refresh_token_and_disconnect(ga_env, monkeypatch):
    """Verify that callback preserves existing encrypted refresh tokens if omitted, and disconnect purges credentials."""
    client = ga_env["client"]
    org_id = str(ga_env["org_a_id"])

    # 1. Setup connection with an existing encrypted token and property
    with ga_env["session_factory"]() as db:
        conn = db.query(IntegrationConnection).filter_by(id="conn-ga-alpha").first()
        conn.config_json = {
            "refresh_token_encrypted": encrypt_secret("initial-stored-refresh-token"),
            "property_id": "properties/123456",
            "property_name": "Initial Site",
        }
        conn.status = "connected"
        db.commit()

    # 2. Initiate OAuth to generate state
    init_res = client.post(
        "/api/v1/oauth/google/authorize?connection_id=conn-ga-alpha",
        headers={"X-Organization-ID": org_id},
    )
    state = init_res.json()["state"]

    # 3. Mock code exchange returning ONLY access_token (NO new refresh_token)
    monkeypatch.setattr(
        "app.api.google_analytics.exchange_authorization_code",
        lambda code, code_verifier: {
            "access_token": "new-short-lived-token",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )

    # 4. Trigger callback with Accept: application/json
    cb_res = client.get(
        f"/api/v1/oauth/google/callback?code=mock-code&state={state}",
        headers={"Accept": "application/json"},
    )
    assert cb_res.status_code == 200
    assert cb_res.json()["connection_status"] == "connected"

    # Verify existing refresh token was preserved
    with ga_env["session_factory"]() as db:
        updated_conn = db.query(IntegrationConnection).filter_by(id="conn-ga-alpha").first()
        assert updated_conn.status == "connected"
        assert "refresh_token_encrypted" in updated_conn.config_json
        from app.core.security import decrypt_secret
        decrypted = decrypt_secret(updated_conn.config_json["refresh_token_encrypted"])
        assert decrypted == "initial-stored-refresh-token"

    # 5. Test disconnect endpoint
    revoked_tokens = []
    class MockRevokeClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, data=None):
            revoked_tokens.append(data.get("token"))
            return Mock(status_code=200)

    monkeypatch.setattr("httpx.Client", MockRevokeClient)

    disc_res = client.post(
        "/api/v1/connections/conn-ga-alpha/disconnect",
        headers={"X-Organization-ID": org_id},
    )
    assert disc_res.status_code == 200
    assert disc_res.json()["status"] == "needs_authorization"

    with ga_env["session_factory"]() as db:
        disc_conn = db.query(IntegrationConnection).filter_by(id="conn-ga-alpha").first()
        assert disc_conn.status == "needs_authorization"
        assert "refresh_token_encrypted" not in disc_conn.config_json
        assert "property_id" not in disc_conn.config_json
        assert "disconnected_at" in disc_conn.config_json


def test_worker_runner_loop_executes_job(ga_env, monkeypatch):
    """Verify that run_worker_loop claims and executes queued jobs in a single pass."""
    # Mock engine in local_worker to use the test engine
    monkeypatch.setattr("app.workers.local_worker.engine", ga_env["engine"])

    # Create a queued job
    with ga_env["session_factory"]() as db:
        now = datetime.now(timezone.utc)
        job = SyncJob(
            id=uuid4(),
            organization_id=ga_env["org_a_id"],
            connection_id="conn-ga-alpha",
            job_type="test_sync",
            status="queued",
            attempt_count=0,
            max_attempts=3,
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        db.commit()
        test_job_id = job.id

    from app.workers.local_worker import DeterministicTestAdapter
    test_adapter = DeterministicTestAdapter(default_result={"records": 42})

    # Run the worker loop with max_jobs=1
    processed_count = run_worker_loop(
        poll_interval_seconds=0.1,
        worker_id="test-runner-worker",
        single_run=True,
        max_jobs=1,
    )
    assert processed_count == 1

    with ga_env["session_factory"]() as db:
        finished_job = db.query(SyncJob).filter_by(id=test_job_id).first()
        assert finished_job.status in ("succeeded", "failed")  # processed by worker


def test_oauth_callback_failure_scenarios_and_atomicity(ga_env, monkeypatch):
    """Verify all failure scenarios: timeout, HTTP error, DB error, and concurrent reuse."""
    client = ga_env["client"]
    org_id = str(ga_env["org_a_id"])

    # 1. Scenario 2: Authorization code exchange returns HTTP error (e.g. 400 invalid_grant)
    with ga_env["session_factory"]() as db:
        st_err = OAuthState(
            state_token="state-code-err",
            code_verifier=encrypt_secret("verifier-code-err"),
            organization_id=ga_env["org_a_id"],
            user_id=ga_env["owner_id"],
            connection_id="conn-ga-alpha",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        db.add(st_err)
        db.commit()

    from app.providers.google_analytics.oauth import GoogleOAuthError
    def mock_exchange_fail(code, code_verifier, client=None):
        raise GoogleOAuthError("Google token exchange failed (400): invalid_grant")

    monkeypatch.setattr(
        "app.api.google_analytics.exchange_authorization_code",
        mock_exchange_fail,
    )

    res_fail = client.get(
        "/api/v1/oauth/google/callback?code=bad-code&state=state-code-err",
        headers={"Accept": "application/json"},
    )
    assert res_fail.status_code == 400
    assert "invalid_grant" in res_fail.json()["detail"]

    # Verify state was marked consumed despite network failure (single-use enforced!)
    with ga_env["session_factory"]() as db:
        consumed_st = db.query(OAuthState).filter_by(state_token="state-code-err").first()
        assert consumed_st.used_at is not None

    # 2. Scenario 3: Token endpoint times out
    with ga_env["session_factory"]() as db:
        st_timeout = OAuthState(
            state_token="state-timeout-err",
            code_verifier=encrypt_secret("verifier-timeout"),
            organization_id=ga_env["org_a_id"],
            user_id=ga_env["owner_id"],
            connection_id="conn-ga-alpha",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        db.add(st_timeout)
        db.commit()

    def mock_exchange_timeout(code, code_verifier, client=None):
        raise GoogleOAuthError("Network error communicating with Google token endpoint: ConnectTimeout")

    monkeypatch.setattr(
        "app.api.google_analytics.exchange_authorization_code",
        mock_exchange_timeout,
    )

    res_timeout = client.get(
        "/api/v1/oauth/google/callback?code=timeout-code&state=state-timeout-err",
        headers={"Accept": "application/json"},
    )
    assert res_timeout.status_code == 400
    assert "ConnectTimeout" in res_timeout.json()["detail"]

    with ga_env["session_factory"]() as db:
        consumed_st = db.query(OAuthState).filter_by(state_token="state-timeout-err").first()
        assert consumed_st.used_at is not None

    # 3. Scenario 5: Database persistence fails after successful token exchange
    with ga_env["session_factory"]() as db:
        st_db_fail = OAuthState(
            state_token="state-db-fail",
            code_verifier=encrypt_secret("verifier-db-fail"),
            organization_id=ga_env["org_a_id"],
            user_id=ga_env["owner_id"],
            connection_id="conn-ga-alpha",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        db.add(st_db_fail)
        db.commit()

    monkeypatch.setattr(
        "app.api.google_analytics.exchange_authorization_code",
        lambda code, code_verifier, client=None: {
            "access_token": "ya29.valid_token",
            "refresh_token": "1//valid_refresh_token",
            "expires_in": 3600,
        },
    )

    # Force database commit error during connection save in step 7
    original_commit = ga_env["session_factory"]

    # We patch Session.commit for the second commit
    from sqlalchemy.orm import Session
    orig_commit = Session.commit
    commit_call_count = 0
    def failing_commit(self):
        nonlocal commit_call_count
        commit_call_count += 1
        # Allow the first commit (state consumption), fail the second commit (connection update)
        if commit_call_count == 2:
            raise RuntimeError("Simulated DB disk failure on connection persistence")
        return orig_commit(self)

    monkeypatch.setattr(Session, "commit", failing_commit)

    res_db_err = client.get(
        "/api/v1/oauth/google/callback?code=db-code&state=state-db-fail",
        headers={"Accept": "application/json"},
    )
    assert res_db_err.status_code == 500
    assert "Failed to persist integration credentials" in res_db_err.json()["detail"]
    # Ensure raw tokens are NOT in response detail
    assert "ya29.valid_token" not in res_db_err.text
    assert "1//valid_refresh_token" not in res_db_err.text

    # Restore commit
    monkeypatch.setattr(Session, "commit", orig_commit)

    # 4. Scenario 7: Concurrent callback consumption race (state consumed by first thread)
    with ga_env["session_factory"]() as db:
        st_race = OAuthState(
            state_token="state-race-condition",
            code_verifier=encrypt_secret("verifier-race"),
            organization_id=ga_env["org_a_id"],
            user_id=ga_env["owner_id"],
            connection_id="conn-ga-alpha",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        db.add(st_race)
        db.commit()

    # Thread 1 consumes state
    res_thread1 = client.get(
        "/api/v1/oauth/google/callback?code=code-1&state=state-race-condition",
        headers={"Accept": "application/json"},
    )
    assert res_thread1.status_code == 200

    # Thread 2 attempting to consume same state is immediately rejected
    res_thread2 = client.get(
        "/api/v1/oauth/google/callback?code=code-2&state=state-race-condition",
        headers={"Accept": "application/json"},
    )
    assert res_thread2.status_code == 400
    assert "already been consumed" in res_thread2.json()["detail"]


def test_google_oauth_authorize_returns_503_without_orphan_state_when_unconfigured(
    ga_env, monkeypatch
):
    client = ga_env["client"]
    org_id = str(ga_env["org_a_id"])
    monkeypatch.setattr(settings, "google_client_id", "")

    res = client.post(
        "/api/v1/oauth/google/authorize",
        headers={"X-Organization-ID": org_id},
        json={"connection_id": "conn-ga-alpha"},
    )
    assert res.status_code == 503
    assert "GOOGLE_CLIENT_ID" in res.json()["detail"]

    with ga_env["session_factory"]() as db:
        states = db.query(OAuthState).filter_by(connection_id="conn-ga-alpha").all()
        assert states == []



