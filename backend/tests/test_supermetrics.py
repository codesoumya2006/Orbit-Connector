from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base, get_db
from app.core.firebase_auth import AuthenticatedPrincipal, get_current_principal
from app.core.security import decrypt_secret, encrypt_secret
from app.main import app
from app.models.entities import (
    AuditEvent,
    IntegrationConnection,
    Organization,
    OrganizationMembership,
    SupermetricsMarketingReport,
    SyncJob,
    User,
)
from app.providers.supermetrics.adapter import SupermetricsAdapter
from app.providers.supermetrics.client import (
    SupermetricsAuthError,
    SupermetricsRateLimitError,
    list_supermetrics_accounts,
    list_supermetrics_data_sources,
    query_supermetrics_data,
    resolve_date_range,
    validate_supermetrics_api_key,
)
from app.providers.supermetrics.constants import (
    SUPERMETRICS_API_BASE_URL,
    SUPERMETRICS_LOGINS_ENDPOINT,
    SUPERMETRICS_QUERY_ENDPOINT,
    SUPPORTED_DATA_SOURCES,
)
from app.workers.local_worker import PermanentSyncError, RetryableSyncError

_real_httpx_client = httpx.Client


@pytest.fixture
def sm_env(monkeypatch):
    fernet_key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "credential_encryption_key", fernet_key)

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
            firebase_uid="uid-sm-owner",
            email="owner@orbit.test",
            email_verified=True,
        )
        viewer = User(
            id=uuid4(),
            firebase_uid="uid-sm-viewer",
            email="viewer@orbit.test",
            email_verified=True,
        )
        other_user = User(
            id=uuid4(),
            firebase_uid="uid-sm-other",
            email="other@orbit.test",
            email_verified=True,
        )
        db.add_all([owner, viewer, other_user])
        db.flush()

        org_a = Organization(id=uuid4(), name="Supermetrics Org A")
        org_b = Organization(id=uuid4(), name="Supermetrics Org B")
        db.add_all([org_a, org_b])
        db.flush()

        db.add(OrganizationMembership(user_id=owner.id, organization_id=org_a.id, role="owner"))
        db.add(OrganizationMembership(user_id=viewer.id, organization_id=org_a.id, role="viewer"))
        db.add(OrganizationMembership(user_id=other_user.id, organization_id=org_b.id, role="owner"))

        conn_a = IntegrationConnection(
            id="conn-sm-alpha",
            organization_id=org_a.id,
            provider_key="supermetrics",
            display_name="Supermetrics Alpha",
            status="needs_configuration",
            connection_type="api_key",
            config_json={},
        )
        conn_b = IntegrationConnection(
            id="conn-sm-beta",
            organization_id=org_b.id,
            provider_key="supermetrics",
            display_name="Supermetrics Beta",
            status="needs_configuration",
            connection_type="api_key",
            config_json={},
        )
        db.add_all([conn_a, conn_b])
        db.commit()

        yield {
            "engine": engine,
            "session_factory": session_factory,
            "owner": owner,
            "viewer": viewer,
            "other_user": other_user,
            "org_a": org_a,
            "org_b": org_b,
            "conn_a": conn_a,
            "conn_b": conn_b,
        }

    app.dependency_overrides.clear()
    engine.dispose()


def test_supermetrics_date_range_validation():
    """Verify ISO date validation and relative keywords (today, yesterday, 30daysAgo)."""
    start, end = resolve_date_range("2026-01-01", "2026-01-31")
    assert start == "2026-01-01"
    assert end == "2026-01-31"

    start_rel, end_rel = resolve_date_range("30daysAgo", "yesterday")
    assert len(start_rel) == 10
    assert len(end_rel) == 10
    assert start_rel < end_rel

    with pytest.raises(ValueError, match="cannot be after"):
        resolve_date_range("2026-02-01", "2026-01-01")

    with pytest.raises(ValueError, match="Invalid date"):
        resolve_date_range("invalid-date-format", "2026-01-01")


def test_supermetrics_client_validation_and_error_classification():
    """Verify API key probe: success, 401 auth error, 429 rate limit, 500 retryable."""
    # 1. Successful key probe
    def mock_logins_ok(request: httpx.Request) -> httpx.Response:
        assert "Bearer valid-sm-key" in request.headers.get("authorization", "")
        return httpx.Response(200, json={"meta": {"status": "ok"}})

    with httpx.Client(transport=httpx.MockTransport(mock_logins_ok)) as client:
        res = validate_supermetrics_api_key("valid-sm-key", client=client)
        assert res["valid"] is True

    # 2. 401 Auth error -> SupermetricsAuthError / PermanentSyncError
    def mock_logins_unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid API key"}})

    with httpx.Client(transport=httpx.MockTransport(mock_logins_unauthorized)) as client:
        with pytest.raises(SupermetricsAuthError, match="Invalid or revoked API key"):
            validate_supermetrics_api_key("bad-key", client=client)

    # 3. 429 Rate limit -> SupermetricsRateLimitError / RetryableSyncError
    def mock_logins_rate_limit(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "Rate limit exceeded"}})

    with httpx.Client(transport=httpx.MockTransport(mock_logins_rate_limit)) as client:
        with pytest.raises(SupermetricsRateLimitError, match="rate limit reached"):
            validate_supermetrics_api_key("valid-key", client=client)


def test_supermetrics_source_and_account_discovery():
    """Verify data source catalogue, login discovery, and account listing via documented sequence."""
    def mock_logins_ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"meta": {"status": "ok"}})

    with httpx.Client(transport=httpx.MockTransport(mock_logins_ok)) as client:
        sources = list_supermetrics_data_sources("test-key", client=client)
        assert len(sources) >= 5
        ds_ids = [s["ds_id"] for s in sources]
        assert "GAWA" in ds_ids
        assert "FB" in ds_ids
        assert "AW" in ds_ids

    # 1. Test Login Discovery with Pagination (GET /enterprise/v2/ds/logins)
    def mock_logins_paginated(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        url_str = str(request.url)
        if "page=2" in url_str:
            return httpx.Response(
                200,
                json={
                    "meta": {"status": 200, "pagination": {"next": None}},
                    "data": [
                        {
                            "login_id": "login_2",
                            "ds_id": "FB",
                            "username": "client2@example.com",
                            "display_name": "Secondary FB Login",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "meta": {
                    "status": 200,
                    "pagination": {"next": "https://api.supermetrics.com/enterprise/v2/ds/logins?ds_id=FB&page=2"},
                },
                "data": [
                    {
                        "login_id": "login_1",
                        "ds_id": "FB",
                        "username": "client1@example.com",
                        "display_name": "Primary FB Login",
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(mock_logins_paginated)) as client:
        from app.providers.supermetrics.client import list_supermetrics_logins
        logins = list_supermetrics_logins("test-key", ds_id="FB", client=client)
        assert len(logins) == 2
        assert logins[0]["login_id"] == "login_1"
        assert logins[1]["login_id"] == "login_2"

    # 2. Test Account Discovery for a specific login (GET /enterprise/v2/ds/login/{login_id}/accounts)
    def mock_login_accounts(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        url_str = str(request.url)
        assert "/ds/login/login_1/accounts" in url_str
        return httpx.Response(
            200,
            json={
                "meta": {"status": 200},
                "data": [
                    {"account_id": "act_111", "account_name": "Main Ad Account", "currency": "USD"},
                    {"account_id": "act_222", "account_name": "EU Ad Account", "currency": "EUR"},
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(mock_login_accounts)) as client:
        from app.providers.supermetrics.client import list_supermetrics_login_accounts
        login_accts = list_supermetrics_login_accounts("test-key", "login_1", client=client)
        assert len(login_accts) == 2
        assert login_accts[0]["account_id"] == "act_111"
        assert login_accts[1]["currency"] == "EUR"

    # 3. Test Full Documented Sequence: logins -> accounts per login
    def mock_full_discovery_sequence(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/ds/logins" in url_str:
            return httpx.Response(
                200,
                json={
                    "meta": {"status": 200},
                    "data": [
                        {"login_id": "login_alpha", "ds_id": "AW"},
                        {"login_id": "login_beta", "ds_id": "AW"},
                    ],
                },
            )
        elif "/ds/login/login_alpha/accounts" in url_str:
            return httpx.Response(
                200,
                json={
                    "meta": {"status": 200},
                    "data": [{"account_id": "111-222-3333", "account_name": "US Search Account", "currency": "USD"}],
                },
            )
        elif "/ds/login/login_beta/accounts" in url_str:
            return httpx.Response(
                200,
                json={
                    "meta": {"status": 200},
                    "data": [{"account_id": "444-555-6666", "account_name": "EU Search Account", "currency": "EUR"}],
                },
            )
        return httpx.Response(404, json={"error": {"message": "Not found"}})

    with httpx.Client(transport=httpx.MockTransport(mock_full_discovery_sequence)) as client:
        accounts = list_supermetrics_accounts("test-key", "AW", client=client)
        assert len(accounts) == 2
        assert accounts[0]["account_id"] == "111-222-3333"
        assert accounts[0]["login_id"] == "login_alpha"
        assert accounts[1]["account_id"] == "444-555-6666"
        assert accounts[1]["login_id"] == "login_beta"


def test_supermetrics_query_and_metric_normalization():
    """Verify query payload format and response normalization for both dict and matrix formats."""
    captured_payload = {}

    # Format A: Dictionary rows
    def mock_query_dict(request: httpx.Request) -> httpx.Response:
        import json
        nonlocal captured_payload
        assert request.url.path == "/enterprise/v2/query/data/json"
        captured_payload = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "date": "2026-11-01",
                        "account_id": "act_111",
                        "campaign_id": "camp_101",
                        "campaign_name": "Black Friday Promo",
                        "impressions": 15000,
                        "clicks": 450,
                        "cost": "123.456789",
                        "conversions": 15.0,
                        "conversions_value": "789.500000",
                        "currency": "EUR",
                    },
                    {
                        "date": "2026-11-02",
                        "account_id": "act_111",
                        "campaign_id": "camp_102",
                        "campaign_name": "Brand Retargeting",
                        "impressions": "5000",
                        "clicks": "120",
                        "cost": "45.000000",
                        "conversions": "3",
                        "conversions_value": "150.00",
                        "currency": "EUR",
                    },
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(mock_query_dict)) as client:
        rows = query_supermetrics_data(
            api_key="mock-key",
            ds_id="FB",
            ds_accounts=["act_111"],
            start_date="2026-11-01",
            end_date="2026-11-02",
            client=client,
        )

    assert captured_payload["ds_id"] == "FB"
    assert captured_payload["ds_accounts"] == "act_111"
    assert captured_payload["start_date"] == "2026-11-01"
    assert captured_payload["end_date"] == "2026-11-02"
    assert isinstance(captured_payload["fields"], list)
    assert captured_payload["max_rows"] == 1000

    assert len(rows) == 2
    r1 = rows[0]
    assert r1.report_date == "2026-11-01"
    assert r1.campaign_name == "Black Friday Promo"
    assert r1.impressions == 15000
    assert r1.clicks == 450
    assert r1.cost == Decimal("123.456789")
    assert isinstance(r1.cost, Decimal)
    assert r1.conversions == 15.0
    assert r1.conversions_value == Decimal("789.500000")
    assert r1.currency == "EUR"

    # Format B: Matrix / array rows with meta.columns
    def mock_query_matrix(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "meta": {
                    "status": 200,
                    "columns": ["Date", "Campaignname", "Impressions", "Clicks", "Cost", "Conversions", "Conversionvalue", "CurrencyCode"],
                },
                "data": [
                    ["2026-11-03", "Matrix Campaign Alpha", 8000, 240, "65.500000", 6.0, "320.000000", "USD"]
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(mock_query_matrix)) as client:
        matrix_rows = query_supermetrics_data(
            api_key="mock-key",
            ds_id="AW",
            ds_accounts="111-222-3333",
            start_date="2026-11-03",
            end_date="2026-11-03",
            client=client,
        )

    assert len(matrix_rows) == 1
    m1 = matrix_rows[0]
    assert m1.report_date == "2026-11-03"
    assert m1.campaign_name == "Matrix Campaign Alpha"
    assert m1.impressions == 8000
    assert m1.clicks == 240
    assert m1.cost == Decimal("65.500000")
    assert m1.conversions == 6.0
    assert m1.conversions_value == Decimal("320.000000")
    assert m1.currency == "USD"


def test_supermetrics_adapter_worker_execution_and_idempotency(sm_env):
    """Verify local worker sync execution: metric insertion, Decimal precision, and idempotent re-sync."""
    env = sm_env
    db = env["session_factory"]()
    conn_a = env["conn_a"]

    # Configure connection with encrypted API key and selected source
    enc_key = encrypt_secret("valid-supermetrics-api-key")
    conn_a.config_json = {
        "api_key_encrypted": enc_key,
        "ds_id": "FB",
        "ds_name": "Facebook Ads",
        "ds_accounts": "act_12345",
        "currency": "EUR",
    }
    conn_a.status = "connected"
    db.commit()

    now = datetime.now(timezone.utc)
    job = SyncJob(
        id=uuid4(),
        organization_id=conn_a.organization_id,
        connection_id=conn_a.id,
        job_type="supermetrics_sync",
        status="running",
        lease_token="lease-sm-1",
        lease_expires_at=now + timedelta(minutes=5),
        metadata_json={"start_date": "2026-11-01", "end_date": "2026-11-02"},
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.commit()

    mock_data = {
        "data": [
            {
                "date": "2026-11-01",
                "account_id": "act_12345",
                "campaign_id": "camp-test-1",
                "campaign_name": "Product Launch",
                "impressions": 1000,
                "clicks": 50,
                "cost": "12.345678",
                "conversions": 3.0,
                "conversions_value": "150.00",
                "currency": "EUR",
            }
        ]
    }

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_data)

    transport = httpx.MockTransport(mock_handler)

    # First sync run
    adapter = SupermetricsAdapter(db=db, client=httpx.Client(transport=transport))
    res = adapter.execute(conn_a, job)
    assert res["rows_synced"] == 1
    assert res["ds_id"] == "FB"

    # Verify report in database
    saved = db.query(SupermetricsMarketingReport).filter_by(connection_id=conn_a.id).all()
    assert len(saved) == 1
    rep = saved[0]
    assert rep.campaign_name == "Product Launch"
    assert rep.impressions == 1000
    assert rep.clicks == 50
    assert rep.cost == Decimal("12.345678")
    assert isinstance(rep.cost, Decimal)
    assert rep.conversions == 3.0
    assert rep.currency == "EUR"

    # Second sync run (idempotent upsert with updated metrics)
    mock_data["data"][0]["impressions"] = 1500
    mock_data["data"][0]["clicks"] = 75
    mock_data["data"][0]["cost"] = "18.500000"

    res_2 = adapter.execute(conn_a, job)
    assert res_2["rows_synced"] == 1

    saved_2 = db.query(SupermetricsMarketingReport).filter_by(connection_id=conn_a.id).all()
    assert len(saved_2) == 1
    rep_2 = saved_2[0]
    assert rep_2.impressions == 1500
    assert rep_2.clicks == 75
    assert rep_2.cost == Decimal("18.500000")

    db.close()


def test_supermetrics_api_endpoints_and_rbac(sm_env):
    """Verify FastAPI routes for API key config, testing, source selection, and RBAC enforcement."""
    env = sm_env
    sf = env["session_factory"]
    org_a = env["org_a"]
    owner = env["owner"]
    viewer = env["viewer"]
    other_user = env["other_user"]
    conn_a = env["conn_a"]

    def override_db():
        with sf() as s:
            yield s

    def auth_owner():
        return AuthenticatedPrincipal(
            uid=owner.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=owner,
        )

    def auth_viewer():
        return AuthenticatedPrincipal(
            uid=viewer.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=viewer,
        )

    def auth_other():
        return AuthenticatedPrincipal(
            uid=other_user.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=other_user,
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_owner

    def mock_probe(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"meta": {"status": "ok"}})

    def mock_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(mock_probe)
        return _real_httpx_client(*args, **kwargs)

    with patch("app.providers.supermetrics.client.httpx.Client", side_effect=mock_factory):
        client = TestClient(app)

        # 1. Owner configures Supermetrics API key (Success)
        res = client.post(
            f"/api/v1/connections/{conn_a.id}/supermetrics/config",
            headers={"X-Organization-ID": str(org_a.id)},
            json={"api_key": "sm_secret_key_12345", "display_name": "SM Global"},
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["status"] == "needs_source_selection"
        assert data["has_secret"] is True

        # Verify key was encrypted at rest in DB
        db = sf()
        conn = db.get(IntegrationConnection, conn_a.id)
        assert "api_key_encrypted" in conn.config_json
        assert decrypt_secret(conn.config_json["api_key_encrypted"]) == "sm_secret_key_12345"
        db.close()

        # 2. Viewer cannot configure API key (403 Forbidden)
        app.dependency_overrides[get_current_principal] = auth_viewer
        res_viewer = client.post(
            f"/api/v1/connections/{conn_a.id}/supermetrics/config",
            headers={"X-Organization-ID": str(org_a.id)},
            json={"api_key": "unauthorized_key"},
        )
        assert res_viewer.status_code == 403

        # 3. Viewer can test connection (read-only)
        res_test = client.post(
            f"/api/v1/connections/{conn_a.id}/supermetrics/test",
            headers={"X-Organization-ID": str(org_a.id)},
        )
        assert res_test.status_code == 200
        assert res_test.json()["valid"] is True

        # 4. Read-only endpoints: logins and accounts
        app.dependency_overrides[get_current_principal] = auth_viewer

        def mock_api_endpoints(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "/ds/logins" in url_str:
                return httpx.Response(
                    200,
                    json={
                        "meta": {"status": 200},
                        "data": [{"login_id": "login_101", "ds_id": "AW", "username": "ads@orbit.test"}],
                    },
                )
            elif "/ds/login/login_101/accounts" in url_str:
                return httpx.Response(
                    200,
                    json={
                        "meta": {"status": 200},
                        "data": [{"account_id": "acc-101", "account_name": "Test Search", "currency": "USD"}],
                    },
                )
            return httpx.Response(200, json={"meta": {"status": "ok"}})

        with patch("app.providers.supermetrics.client.httpx.Client", side_effect=lambda *a, **k: _real_httpx_client(transport=httpx.MockTransport(mock_api_endpoints))):
            res_logins = client.get(
                f"/api/v1/connections/{conn_a.id}/supermetrics/logins?ds_id=AW",
                headers={"X-Organization-ID": str(org_a.id)},
            )
            assert res_logins.status_code == 200
            assert res_logins.json()["logins"][0]["login_id"] == "login_101"

            res_login_accts = client.get(
                f"/api/v1/connections/{conn_a.id}/supermetrics/logins/login_101/accounts",
                headers={"X-Organization-ID": str(org_a.id)},
            )
            assert res_login_accts.status_code == 200
            assert res_login_accts.json()["accounts"][0]["account_id"] == "acc-101"

            res_accts = client.get(
                f"/api/v1/connections/{conn_a.id}/supermetrics/accounts?ds_id=AW",
                headers={"X-Organization-ID": str(org_a.id)},
            )
            assert res_accts.status_code == 200
            assert len(res_accts.json()["accounts"]) == 1

        # 5. Cross-tenant access denied (fail-closed 404)
        app.dependency_overrides[get_current_principal] = auth_other
        res_cross = client.post(
            f"/api/v1/connections/{conn_a.id}/supermetrics/config",
            headers={"X-Organization-ID": str(org_a.id)},
            json={"api_key": "sm_hack"},
        )
        assert res_cross.status_code in (403, 404)


def test_supermetrics_reports_and_disconnect(sm_env):
    """Verify report retrieval, metric summaries, and disconnect cleanup."""
    env = sm_env
    sf = env["session_factory"]
    org_a = env["org_a"]
    owner = env["owner"]
    viewer = env["viewer"]
    conn_a = env["conn_a"]

    def override_db():
        with sf() as s:
            yield s

    def auth_owner():
        return AuthenticatedPrincipal(
            uid=owner.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=owner,
        )

    def auth_viewer():
        return AuthenticatedPrincipal(
            uid=viewer.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=viewer,
        )

    db = sf()
    conn = db.get(IntegrationConnection, conn_a.id)
    conn.status = "connected"
    conn.config_json = {
        "api_key_encrypted": encrypt_secret("stored-key"),
        "ds_id": "AW",
        "ds_name": "Google Ads",
        "ds_accounts": "111-222-3333",
        "currency": "EUR",
    }
    now = datetime.now(timezone.utc)
    r1 = SupermetricsMarketingReport(
        id=uuid4(),
        organization_id=org_a.id,
        connection_id=conn.id,
        ds_id="AW",
        ds_account_id="111-222-3333",
        report_date="2026-11-01",
        campaign_id="camp-1",
        campaign_name="AdWords Campaign 1",
        impressions=1000,
        clicks=50,
        cost=Decimal("25.500000"),
        conversions=2.0,
        conversions_value=Decimal("100.000000"),
        currency="EUR",
        created_at=now,
        updated_at=now,
    )
    r2 = SupermetricsMarketingReport(
        id=uuid4(),
        organization_id=org_a.id,
        connection_id=conn.id,
        ds_id="AW",
        ds_account_id="111-222-3333",
        report_date="2026-11-02",
        campaign_id="camp-2",
        campaign_name="AdWords Campaign 2",
        impressions=2000,
        clicks=100,
        cost=Decimal("50.000000"),
        conversions=4.0,
        conversions_value=Decimal("200.000000"),
        currency="EUR",
        created_at=now,
        updated_at=now,
    )
    db.add_all([r1, r2])
    db.commit()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_viewer

    client = TestClient(app)

    # 1. Query reports as Viewer
    res = client.get(
        f"/api/v1/connections/{conn.id}/supermetrics/reports",
        headers={"X-Organization-ID": str(org_a.id)},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert len(data["items"]) == 2
    assert data["total"] == 2
    assert data["summary"]["currency"] == "EUR"
    assert data["summary"]["total_impressions"] == 3000
    assert data["summary"]["total_clicks"] == 150
    assert Decimal(str(data["summary"]["total_cost"])) == Decimal("75.500000")
    assert data["summary"]["total_conversions"] == 6.0

    # 2. Secret Redaction: Verify /connections/{id} endpoint never leaks API key
    conn_res = client.get(
        f"/api/v1/connections/{conn.id}",
        headers={"X-Organization-ID": str(org_a.id)},
    )
    assert conn_res.status_code == 200
    conn_data = conn_res.json()
    assert "api_key" not in conn_data
    assert "api_key_encrypted" not in conn_data
    assert conn_data["has_secret"] is True

    # 3. Disconnect as Owner
    app.dependency_overrides[get_current_principal] = auth_owner
    res_disc = client.post(
        f"/api/v1/connections/{conn.id}/supermetrics/disconnect",
        headers={"X-Organization-ID": str(org_a.id)},
    )
    assert res_disc.status_code == 200
    assert res_disc.json()["status"] == "disconnected"

    db.refresh(conn)
    assert conn.status == "disconnected"
    assert "api_key_encrypted" not in conn.config_json

    db.close()

