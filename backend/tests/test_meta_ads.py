from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_real_httpx_client = httpx.Client

from app.core.config import settings
from app.core.database import Base, get_db
from app.core.firebase_auth import AuthenticatedPrincipal, get_current_principal
from app.core.security import decrypt_secret, encrypt_secret
from app.main import app
from app.models.entities import (
    AuditEvent,
    IntegrationConnection,
    MetaAdsCampaignReport,
    OAuthState,
    Organization,
    OrganizationMembership,
    SyncJob,
    User,
)
from app.providers.meta_ads.adapter import MetaAdsAdapter
from app.providers.meta_ads.constants import (
    META_ADS_API_VERSION,
    META_ADS_SCOPE,
    META_GRAPH_BASE_URL,
    META_OAUTH_DIALOG_URL,
    META_OAUTH_TOKEN_URL,
)
from app.providers.meta_ads.account import (
    MetaAdsAccountError,
    clean_ad_account_id,
    format_act_id,
    list_accessible_ad_accounts,
    validate_ad_account_access,
)
from app.providers.meta_ads.oauth import (
    MetaAdsOAuthError,
    build_meta_ads_authorization_url,
    exchange_authorization_code,
    exchange_for_long_lived_token,
    revoke_meta_token,
)
from app.providers.meta_ads.insights import (
    NormalizedMetaCampaignRow,
    fetch_meta_ads_campaign_insights,
    parse_action_metrics,
    resolve_report_dates,
)
from app.workers.local_worker import (
    PermanentSyncError,
    RetryableSyncError,
    SyncWorkerError,
    process_claimed_job,
)


@pytest.fixture(autouse=True)
def meta_ads_env(monkeypatch):
    fernet_key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "credential_encryption_key", fernet_key)
    monkeypatch.setattr(settings, "meta_app_id", "test-meta-app-id-12345")
    monkeypatch.setattr(settings, "meta_app_secret", "test-meta-app-secret-abcde")
    monkeypatch.setattr(settings, "meta_redirect_uri", "http://localhost:8000/api/v1/oauth/meta-ads/callback")
    monkeypatch.setattr(settings, "meta_ads_api_version", "v27.0")

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
            firebase_uid="uid-meta-owner",
            email="owner@orbit.test",
            email_verified=True,
        )
        viewer = User(
            id=uuid4(),
            firebase_uid="uid-meta-viewer",
            email="viewer@orbit.test",
            email_verified=True,
        )
        other_user = User(
            id=uuid4(),
            firebase_uid="uid-meta-other",
            email="other@orbit.test",
            email_verified=True,
        )
        db.add_all([owner, viewer, other_user])
        db.flush()

        org_a = Organization(id=uuid4(), name="Meta Org A")
        org_b = Organization(id=uuid4(), name="Meta Org B")
        db.add_all([org_a, org_b])
        db.flush()

        db.add(OrganizationMembership(user_id=owner.id, organization_id=org_a.id, role="owner"))
        db.add(OrganizationMembership(user_id=viewer.id, organization_id=org_a.id, role="viewer"))
        db.add(OrganizationMembership(user_id=other_user.id, organization_id=org_b.id, role="owner"))

        conn_a = IntegrationConnection(
            id="conn-meta-alpha",
            organization_id=org_a.id,
            provider_key="meta_ads",
            display_name="Meta Ads Alpha",
            status="needs_authorization",
            connection_type="oauth",
            config_json={},
        )
        conn_b = IntegrationConnection(
            id="conn-meta-beta",
            organization_id=org_b.id,
            provider_key="meta_ads",
            display_name="Meta Ads Beta",
            status="needs_authorization",
            connection_type="oauth",
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


def test_meta_ads_constants_and_version_centralization():
    """Verify Meta Ads API version v27.0 centralization across constants and settings."""
    assert META_ADS_API_VERSION == "v27.0"
    assert META_GRAPH_BASE_URL == "https://graph.facebook.com/v27.0"
    assert META_OAUTH_DIALOG_URL == "https://www.facebook.com/v27.0/dialog/oauth"
    assert META_OAUTH_TOKEN_URL == "https://graph.facebook.com/v27.0/oauth/access_token"
    assert META_ADS_SCOPE == "ads_read"
    assert settings.meta_ads_api_version == "v27.0"


def test_clean_and_format_ad_account_id():
    """Verify ad account ID normalization and formatting (stripping and adding act_)."""
    assert clean_ad_account_id("act_1234567890") == "1234567890"
    assert clean_ad_account_id("act_ 987654321 ") == "987654321"
    assert clean_ad_account_id("1234567890") == "1234567890"
    assert clean_ad_account_id(" 555666777 ") == "555666777"

    assert format_act_id("1234567890") == "act_1234567890"
    assert format_act_id("act_1234567890") == "act_1234567890"

    with pytest.raises(MetaAdsAccountError, match="Must contain numeric digits"):
        clean_ad_account_id("invalid-act-id")

    with pytest.raises(MetaAdsAccountError, match="cannot be empty"):
        clean_ad_account_id("")


def test_oauth_authorization_url_generation():
    """Verify OAuth authorization URL formatting with client_id, state, redirect_uri, and ads_read scope."""
    url = build_meta_ads_authorization_url(state="test-crypto-state")
    assert url.startswith("https://www.facebook.com/v27.0/dialog/oauth")
    assert "client_id=" in url
    assert "state=test-crypto-state" in url
    assert "scope=ads_read" in url
    assert "response_type=code" in url
    assert "client_secret" not in url


def test_oauth_code_and_long_lived_token_exchange():
    """Verify short-lived code exchange followed by long-lived token exchange."""
    calls = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "code=" in str(request.url) and "grant_type" not in str(request.url):
            return httpx.Response(
                200,
                json={"access_token": "short-lived-token-xyz", "token_type": "bearer", "expires_in": 3600},
            )
        if "grant_type=fb_exchange_token" in str(request.url):
            return httpx.Response(
                200,
                json={"access_token": "long-lived-token-60days-abc", "token_type": "bearer", "expires_in": 5184000},
            )
        return httpx.Response(400)

    transport = httpx.MockTransport(mock_handler)
    with httpx.Client(transport=transport) as client:
        short_res = exchange_authorization_code(code="auth-code-123", client=client)
        assert short_res["access_token"] == "short-lived-token-xyz"

        long_res = exchange_for_long_lived_token(
            short_lived_token=short_res["access_token"],
            client=client,
        )
        assert long_res["access_token"] == "long-lived-token-60days-abc"
        assert long_res["expires_in"] == 5184000

    assert len(calls) == 2


def test_ad_account_discovery_with_cursor_pagination():
    """Verify discovery of accessible ad accounts with cursor-based pagination."""
    requests_made = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(str(request.url))
        if "after=" in str(request.url):
            # Page 2
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "act_333333333",
                            "account_id": "333333333",
                            "name": "Page 2 Ad Account",
                            "currency": "EUR",
                            "account_status": 1,
                        }
                    ],
                    "paging": {"cursors": {"after": "page_2_end"}},
                },
            )
        else:
            # Page 1
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "act_111111111",
                            "account_id": "111111111",
                            "name": "Primary Ad Account",
                            "currency": "USD",
                            "account_status": 1,
                        },
                        {
                            "id": "act_222222222",
                            "account_id": "222222222",
                            "name": "Secondary Ad Account",
                            "currency": "USD",
                            "account_status": 1,
                        },
                    ],
                    "paging": {
                        "cursors": {"after": "cursor_next_page"},
                        "next": "https://graph.facebook.com/v27.0/me/adaccounts?after=cursor_next_page",
                    },
                },
            )

    transport = httpx.MockTransport(mock_handler)
    with httpx.Client(transport=transport) as client:
        accounts = list_accessible_ad_accounts("mock-token", client=client)

    assert len(accounts) == 3
    assert accounts[0]["account_id"] == "111111111"
    assert accounts[0]["name"] == "Primary Ad Account"
    assert accounts[0]["currency"] == "USD"
    assert accounts[1]["account_id"] == "222222222"
    assert accounts[2]["account_id"] == "333333333"
    assert accounts[2]["currency"] == "EUR"
    assert len(requests_made) == 2


def test_fetch_meta_ads_campaign_insights_with_metrics_and_actions():
    """Verify campaign insights retrieval with daily breakdown, metric parsing, exact Decimal spend, and pagination."""
    requests_made = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(str(request.url))
        assert "level=campaign" in str(request.url)
        assert "time_increment=1" in str(request.url)
        assert "time_range=" in str(request.url)

        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "date_start": "2026-11-01",
                        "account_id": "123456789",
                        "campaign_id": "camp_101",
                        "campaign_name": "Summer Retargeting",
                        "objective": "OUTCOME_SALES",
                        "impressions": "15000",
                        "reach": "12000",
                        "clicks": "450",
                        "spend": "123.456789",
                        "ctr": "3.00",
                        "cpc": "0.274348",
                        "cpm": "8.230452",
                        "frequency": "1.25",
                        "actions": [
                            {"action_type": "purchase", "value": "12"},
                            {"action_type": "lead", "value": "5"},
                            {"action_type": "link_click", "value": "450"},
                        ],
                        "action_values": [
                            {"action_type": "purchase", "value": "789.50"},
                        ],
                    },
                    {
                        "date_start": "2026-11-02",
                        "account_id": "123456789",
                        "campaign_id": "camp_102",
                        "campaign_name": "Brand Awareness",
                        "objective": "OUTCOME_AWARENESS",
                        "impressions": "50000",
                        "reach": "40000",
                        "clicks": "200",
                        "spend": "50.00",
                        "ctr": "0.40",
                        "cpc": "0.25",
                        "cpm": "1.00",
                        "frequency": "1.25",
                        "actions": [],
                        "action_values": [],
                    },
                ],
                "paging": {"cursors": {"after": "end"}},
            },
        )

    transport = httpx.MockTransport(mock_handler)
    with httpx.Client(transport=transport) as client:
        rows = fetch_meta_ads_campaign_insights(
            ad_account_id="123456789",
            access_token="mock-token",
            start_date="2026-11-01",
            end_date="2026-11-02",
            client=client,
        )

    assert len(rows) == 2
    r1 = rows[0]
    assert r1.campaign_id == "camp_101"
    assert r1.campaign_name == "Summer Retargeting"
    assert r1.campaign_status == "OUTCOME_SALES"
    assert r1.impressions == 15000
    assert r1.reach == 12000
    assert r1.clicks == 450
    assert r1.spend == Decimal("123.456789")
    assert isinstance(r1.spend, Decimal)
    assert r1.conversions == 17.0  # 12 purchases + 5 leads
    assert r1.conversions_value == Decimal("789.50")

    r2 = rows[1]
    assert r2.campaign_id == "camp_102"
    assert r2.spend == Decimal("50.00")
    assert r2.conversions == 0.0
    assert r2.conversions_value == Decimal("0.000000")


def test_meta_ads_api_error_classification():
    """Verify rate limits classify as RetryableSyncError and token expiry/invalid account classify as PermanentSyncError."""
    # 1. Rate limiting error (error code 17)
    def mock_rate_limit(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": 17, "message": "User request limit reached", "type": "OAuthException"}},
        )

    with httpx.Client(transport=httpx.MockTransport(mock_rate_limit)) as client:
        with pytest.raises(RetryableSyncError, match="rate limit"):
            fetch_meta_ads_campaign_insights(access_token="mock-token", account_id="123456789", start_date="2026-11-01", end_date="2026-11-02", client=client)

    # 2. Token expired (error code 190)
    def mock_token_expired(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": 190, "message": "Error validating access token: Session has expired", "type": "OAuthException"}},
        )

    with httpx.Client(transport=httpx.MockTransport(mock_token_expired)) as client:
        with pytest.raises(PermanentSyncError, match="permanent authorization failure"):
            fetch_meta_ads_campaign_insights(access_token="mock-token", account_id="123456789", start_date="2026-11-01", end_date="2026-11-02", client=client)

    # 3. Invalid account id (error code 100)
    def mock_invalid_account(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": 100, "message": "Unsupported get request on object", "type": "GraphMethodException"}},
        )

    with httpx.Client(transport=httpx.MockTransport(mock_invalid_account)) as client:
        with pytest.raises(PermanentSyncError, match="Meta Ads Insights request failed"):
            fetch_meta_ads_campaign_insights(access_token="mock-token", account_id="123456789", start_date="2026-11-01", end_date="2026-11-02", client=client)


def test_meta_ads_adapter_worker_execution_and_idempotency(meta_ads_env):
    """Verify local worker sync execution: metric insertion, Decimal precision, and idempotent re-sync."""
    env = meta_ads_env
    db = env["session_factory"]()
    conn_a = env["conn_a"]

    # Configure connection with encrypted access token and selected account
    enc_token = encrypt_secret("valid-long-lived-meta-token")
    conn_a.config_json = {
        "access_token_encrypted": enc_token,
        "ad_account_id": "123456789",
        "ad_account_name": "Test Ad Account",
        "currency": "USD",
    }
    conn_a.status = "connected"
    db.commit()

    # Create SyncJob
    now = datetime.now(timezone.utc)
    job = SyncJob(
        id=uuid4(),
        organization_id=conn_a.organization_id,
        connection_id=conn_a.id,
        job_type="meta_ads_sync",
        status="running",
        lease_token="lease-meta-1",
        lease_expires_at=now + timedelta(minutes=5),
        metadata_json={"start_date": "2026-11-01", "end_date": "2026-11-02"},
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.commit()

    # Mock insights API
    mock_data = {
        "data": [
            {
                "date_start": "2026-11-01",
                "account_id": "123456789",
                "campaign_id": "camp-test-1",
                "campaign_name": "Product Launch",
                "objective": "OUTCOME_SALES",
                "impressions": "1000",
                "reach": "800",
                "clicks": "50",
                "spend": "12.345678",
                "ctr": "5.0",
                "cpc": "0.246914",
                "cpm": "12.345678",
                "frequency": "1.25",
                "actions": [{"action_type": "purchase", "value": "3"}],
                "action_values": [{"action_type": "purchase", "value": "150.00"}],
            }
        ],
        "paging": {"cursors": {"after": "end"}},
    }

    def mock_insights_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_data)

    transport = httpx.MockTransport(mock_insights_handler)

    # First sync run
    adapter = MetaAdsAdapter(db=db, client=httpx.Client(transport=transport))
    res = adapter.execute(conn_a, job)
    assert res["rows_synced"] == 1

    # Verify report in database
    saved_reports = db.query(MetaAdsCampaignReport).filter_by(connection_id=conn_a.id).all()
    assert len(saved_reports) == 1
    rep = saved_reports[0]
    assert rep.campaign_name == "Product Launch"
    assert rep.impressions == 1000
    assert rep.clicks == 50
    assert rep.spend == Decimal("12.345678")
    assert isinstance(rep.spend, Decimal)
    assert rep.conversions == 3.0
    assert rep.conversions_value == Decimal("150.000000")

    # Second sync run (idempotent upsert with updated metrics)
    mock_data["data"][0]["impressions"] = "1500"
    mock_data["data"][0]["clicks"] = "75"
    mock_data["data"][0]["spend"] = "18.500000"

    res_2 = adapter.execute(conn_a, job)
    assert res_2["rows_synced"] == 1

    # Verify record count is still 1 and metrics updated
    saved_reports_2 = db.query(MetaAdsCampaignReport).filter_by(connection_id=conn_a.id).all()
    assert len(saved_reports_2) == 1
    rep_updated = saved_reports_2[0]
    assert rep_updated.impressions == 1500
    assert rep_updated.clicks == 75
    assert rep_updated.spend == Decimal("18.500000")

    db.close()


def test_meta_ads_api_endpoints_and_rbac(meta_ads_env):
    """Verify FastAPI routes for OAuth initiation, account selection, report reading, and RBAC enforcement."""
    env = meta_ads_env
    sf = env["session_factory"]
    org_a = env["org_a"]
    org_b = env["org_b"]
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

    client = TestClient(app)

    # 1. Initiate OAuth as Owner (Success)
    res = client.post(
        f"/api/v1/oauth/meta-ads/authorize?connection_id={conn_a.id}",
        headers={"X-Organization-ID": str(org_a.id)},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert "authorization_url" in data
    assert "state" in data
    assert "dialog/oauth" in data["authorization_url"]

    # 2. Initiate OAuth as Viewer (Forbidden)
    app.dependency_overrides[get_current_principal] = auth_viewer
    res_viewer = client.post(
        f"/api/v1/oauth/meta-ads/authorize?connection_id={conn_a.id}",
        headers={"X-Organization-ID": str(org_a.id)},
    )
    assert res_viewer.status_code == 403

    # 3. Cross-tenant access attempt (404 anti-enumeration fail-closed)
    app.dependency_overrides[get_current_principal] = auth_other
    res_cross = client.post(
        f"/api/v1/oauth/meta-ads/authorize?connection_id={conn_a.id}",
        headers={"X-Organization-ID": str(org_a.id)},
    )
    assert res_cross.status_code in (403, 404)


def test_oauth_callback_flow_and_token_protection(meta_ads_env):
    """Verify callback exchanges authorization code for long-lived token, stores encrypted, and redacts secrets."""
    env = meta_ads_env
    session_factory = env["session_factory"]
    org_a = env["org_a"]
    owner = env["owner"]
    conn_a = env["conn_a"]

    def override_db():
        with session_factory() as s:
            yield s

    db = session_factory()
    conn = db.get(IntegrationConnection, conn_a.id)
    now = datetime.now(timezone.utc)
    oauth_state = OAuthState(
        state_token="meta-test-state-token",
        code_verifier=encrypt_secret("mock-verifier"),
        organization_id=org_a.id,
        user_id=owner.id,
        connection_id=conn.id,
        expires_at=now + timedelta(minutes=10),
        created_at=now,
    )
    db.add(oauth_state)
    db.commit()

    def mock_fb_token_exchange(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "code=" in url_str and "grant_type" not in url_str:
            return httpx.Response(
                200,
                json={"access_token": "short-code-token", "token_type": "bearer", "expires_in": 3600},
            )
        if "grant_type=fb_exchange_token" in url_str:
            return httpx.Response(
                200,
                json={"access_token": "long-exchange-token-secret-xyz", "token_type": "bearer", "expires_in": 5184000},
            )
        return httpx.Response(400)

    app.dependency_overrides[get_db] = override_db

    def mock_fb_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(mock_fb_token_exchange)
        return _real_httpx_client(*args, **kwargs)

    with patch("app.providers.meta_ads.oauth.httpx.Client", side_effect=mock_fb_client):
        client = TestClient(app)
        res = client.get(
            "/api/v1/oauth/meta-ads/callback?code=valid-meta-auth-code&state=meta-test-state-token",
            follow_redirects=False,
        )
        assert res.status_code == 307
        assert "meta_ads_connected=true" in res.headers["location"]

    # Verify state consumed
    db.refresh(oauth_state)
    assert oauth_state.used_at is not None

    # Verify connection updated and token stored encrypted
    db.refresh(conn)
    assert conn.status == "needs_account_selection"
    assert "access_token_encrypted" in conn.config_json
    encrypted_token = conn.config_json["access_token_encrypted"]
    decrypted = decrypt_secret(encrypted_token)
    assert decrypted == "long-exchange-token-secret-xyz"

    # Verify connection endpoint does not leak secrets
    def auth_owner():
        return AuthenticatedPrincipal(
            uid=owner.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=owner,
        )
    app.dependency_overrides[get_current_principal] = auth_owner

    conn_res = client.get(
        f"/api/v1/connections/{conn.id}",
        headers={"X-Organization-ID": str(org_a.id)},
    )
    assert conn_res.status_code == 200
    conn_data = conn_res.json()
    assert "access_token" not in conn_data
    assert "access_token_encrypted" not in conn_data
    assert conn_data["has_secret"] is True

    # Attempt replay with consumed state (must fail)
    res_replay = client.get(
        "/api/v1/oauth/meta-ads/callback?code=valid-meta-auth-code&state=meta-test-state-token",
        follow_redirects=False,
    )
    assert res_replay.status_code == 400

    db.close()


def test_meta_ads_reports_endpoint_and_summary(meta_ads_env):
    """Verify GET /connections/{id}/meta-ads/reports returns items and accurate metric summaries."""
    env = meta_ads_env
    session_factory = env["session_factory"]
    org_a = env["org_a"]
    viewer = env["viewer"]
    conn_a = env["conn_a"]

    def override_db():
        with session_factory() as s:
            yield s

    def auth_viewer():
        return AuthenticatedPrincipal(
            uid=viewer.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=viewer,
        )

    db = session_factory()
    conn = db.get(IntegrationConnection, conn_a.id)
    conn.status = "connected"
    conn.config_json = {
        "ad_account_id": "123456789",
        "ad_account_name": "Test Account",
        "currency": "EUR",
    }
    now = datetime.now(timezone.utc)
    r1 = MetaAdsCampaignReport(
        id=uuid4(),
        organization_id=org_a.id,
        connection_id=conn.id,
        ad_account_id="123456789",
        report_date="2026-11-01",
        campaign_id="camp-1",
        campaign_name="Campaign 1",
        campaign_status="ACTIVE",
        impressions=1000,
        reach=800,
        clicks=50,
        spend=Decimal("20.000000"),
        ctr=5.0,
        cpc=Decimal("0.400000"),
        cpm=Decimal("20.000000"),
        frequency=1.25,
        conversions=2.0,
        conversions_value=Decimal("80.000000"),
        created_at=now,
        updated_at=now,
    )
    r2 = MetaAdsCampaignReport(
        id=uuid4(),
        organization_id=org_a.id,
        connection_id=conn.id,
        ad_account_id="123456789",
        report_date="2026-11-02",
        campaign_id="camp-2",
        campaign_name="Campaign 2",
        campaign_status="PAUSED",
        impressions=2000,
        reach=1500,
        clicks=100,
        spend=Decimal("30.000000"),
        ctr=5.0,
        cpc=Decimal("0.300000"),
        cpm=Decimal("15.000000"),
        frequency=1.33,
        conversions=3.0,
        conversions_value=Decimal("120.000000"),
        created_at=now,
        updated_at=now,
    )
    db.add_all([r1, r2])
    db.commit()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_viewer

    client = TestClient(app)

    res = client.get(
        f"/api/v1/connections/{conn.id}/meta-ads/reports",
        headers={"X-Organization-ID": str(org_a.id)},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert len(data["items"]) == 2
    assert data["total"] == 2

    summary = data["summary"]
    assert summary["currency"] == "EUR"
    assert summary["total_impressions"] == 3000
    assert summary["total_reach"] == 2300
    assert summary["total_clicks"] == 150
    assert Decimal(str(summary["total_spend"])) == Decimal("50.000000")
    assert summary["total_conversions"] == 5.0
    assert Decimal(str(summary["total_conversions_value"])) == Decimal("200.000000")

    db.close()


def test_meta_ads_disconnect(meta_ads_env):
    """Verify disconnection revokes token, deletes credentials, and resets status."""
    env = meta_ads_env
    session_factory = env["session_factory"]
    org_a = env["org_a"]
    owner = env["owner"]
    conn_a = env["conn_a"]

    def override_db():
        with session_factory() as s:
            yield s

    def auth_owner():
        return AuthenticatedPrincipal(
            uid=owner.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=owner,
        )

    db = session_factory()
    conn = db.get(IntegrationConnection, conn_a.id)
    enc_token = encrypt_secret("token-to-revoke")
    conn.config_json = {
        "access_token_encrypted": enc_token,
        "ad_account_id": "123456789",
    }
    conn.status = "connected"
    db.commit()

    def mock_revoke(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert "/me/permissions" in str(request.url)
        return httpx.Response(200, json={"success": True})

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_owner

    def mock_revoke_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(mock_revoke)
        return _real_httpx_client(*args, **kwargs)

    with patch("app.providers.meta_ads.oauth.httpx.Client", side_effect=mock_revoke_client):
        client = TestClient(app)

        res = client.post(
            f"/api/v1/connections/{conn.id}/meta-ads/disconnect",
            headers={"X-Organization-ID": str(org_a.id)},
        )
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "disconnected"

    db.refresh(conn)
    assert conn.status == "disconnected"
    assert "access_token_encrypted" not in conn.config_json

    db.close()


def test_meta_ads_currency_separation_and_explicit_missing_handling(meta_ads_env):
    """Verify that reports never cross-combine different currencies, and missing currency fails explicitly."""
    env = meta_ads_env
    session_factory = env["session_factory"]
    org_a = env["org_a"]
    owner = env["owner"]
    viewer = env["viewer"]
    conn_a = env["conn_a"]

    def override_db():
        with session_factory() as s:
            yield s

    def auth_viewer():
        return AuthenticatedPrincipal(
            uid=viewer.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=viewer,
        )

    db = session_factory()
    now = datetime.now(timezone.utc)

    # 1. Setup Connection A (EUR) with account 111111111
    conn_eur = db.get(IntegrationConnection, conn_a.id)
    conn_eur.status = "connected"
    conn_eur.config_json = {
        "ad_account_id": "111111111",
        "ad_account_name": "EUR Ad Account",
        "currency": "EUR",
    }

    # 2. Setup Connection B in Org A (GBP) with account 222222222
    conn_gbp = IntegrationConnection(
        id="conn-meta-gbp",
        organization_id=org_a.id,
        provider_key="meta_ads",
        display_name="Meta Ads GBP",
        status="connected",
        connection_type="oauth",
        config_json={
            "ad_account_id": "222222222",
            "ad_account_name": "GBP Ad Account",
            "currency": "GBP",
        },
        created_at=now,
    )
    db.add(conn_gbp)

    # 3. Setup Connection C in Org A with missing currency metadata
    conn_missing_curr = IntegrationConnection(
        id="conn-meta-no-curr",
        organization_id=org_a.id,
        provider_key="meta_ads",
        display_name="Meta Ads No Currency",
        status="connected",
        connection_type="oauth",
        config_json={
            "ad_account_id": "333333333",
            "ad_account_name": "No Currency Account",
        },
        created_at=now,
    )
    db.add(conn_missing_curr)

    # 4. Seed reports for EUR account
    r_eur = MetaAdsCampaignReport(
        id=uuid4(),
        organization_id=org_a.id,
        connection_id=conn_eur.id,
        ad_account_id="111111111",
        report_date="2026-11-01",
        campaign_id="camp-eur-1",
        campaign_name="EUR Campaign",
        campaign_status="ACTIVE",
        impressions=1000,
        reach=800,
        clicks=50,
        spend=Decimal("75.500000"),
        ctr=5.0,
        cpc=Decimal("1.510000"),
        cpm=Decimal("75.500000"),
        frequency=1.25,
        conversions=5.0,
        conversions_value=Decimal("250.000000"),
        created_at=now,
        updated_at=now,
    )

    # 5. Seed reports for GBP account
    r_gbp = MetaAdsCampaignReport(
        id=uuid4(),
        organization_id=org_a.id,
        connection_id=conn_gbp.id,
        ad_account_id="222222222",
        report_date="2026-11-01",
        campaign_id="camp-gbp-1",
        campaign_name="GBP Campaign",
        campaign_status="ACTIVE",
        impressions=2000,
        reach=1500,
        clicks=80,
        spend=Decimal("120.250000"),
        ctr=4.0,
        cpc=Decimal("1.503125"),
        cpm=Decimal("60.125000"),
        frequency=1.33,
        conversions=8.0,
        conversions_value=Decimal("400.000000"),
        created_at=now,
        updated_at=now,
    )

    db.add_all([r_eur, r_gbp])
    db.commit()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_viewer

    client = TestClient(app)

    # Query EUR connection: must isolate EUR spend and never include GBP spend
    res_eur = client.get(
        f"/api/v1/connections/{conn_eur.id}/meta-ads/reports",
        headers={"X-Organization-ID": str(org_a.id)},
    )
    assert res_eur.status_code == 200, res_eur.text
    data_eur = res_eur.json()
    assert len(data_eur["items"]) == 1
    assert data_eur["items"][0]["ad_account_id"] == "111111111"
    assert data_eur["summary"]["currency"] == "EUR"
    assert data_eur["summary"]["currency_code"] == "EUR"
    assert Decimal(str(data_eur["summary"]["total_spend"])) == Decimal("75.500000")

    # Query GBP connection: must isolate GBP spend and never include EUR spend
    res_gbp = client.get(
        f"/api/v1/connections/{conn_gbp.id}/meta-ads/reports",
        headers={"X-Organization-ID": str(org_a.id)},
    )
    assert res_gbp.status_code == 200, res_gbp.text
    data_gbp = res_gbp.json()
    assert len(data_gbp["items"]) == 1
    assert data_gbp["items"][0]["ad_account_id"] == "222222222"
    assert data_gbp["summary"]["currency"] == "GBP"
    assert data_gbp["summary"]["currency_code"] == "GBP"
    assert Decimal(str(data_gbp["summary"]["total_spend"])) == Decimal("120.250000")

    # Query connection with missing currency: must fail explicitly with HTTP 400, not silently default to USD
    res_no_curr = client.get(
        f"/api/v1/connections/{conn_missing_curr.id}/meta-ads/reports",
        headers={"X-Organization-ID": str(org_a.id)},
    )
    assert res_no_curr.status_code == 400
    assert "missing currency metadata" in res_no_curr.text.lower()

    db.close()


def test_meta_ads_oauth_authorize_returns_503_without_orphan_state_when_unconfigured(
    meta_ads_env, monkeypatch
):
    session_factory = meta_ads_env["session_factory"]
    owner = meta_ads_env["owner"]
    org_a = meta_ads_env["org_a"]
    conn_a = meta_ads_env["conn_a"]

    def override_db():
        with session_factory() as s:
            yield s

    def auth_owner():
        return AuthenticatedPrincipal(
            uid=owner.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=owner,
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_owner
    monkeypatch.setattr(settings, "meta_app_id", "")

    client = TestClient(app)
    res = client.post(
        "/api/v1/oauth/meta-ads/authorize",
        headers={"X-Organization-ID": str(org_a.id)},
        json={"connection_id": conn_a.id},
    )
    assert res.status_code == 503
    assert "META_APP_ID" in res.json()["detail"]

    with session_factory() as db:
        assert db.query(OAuthState).filter_by(connection_id=conn_a.id).all() == []


def test_meta_ads_direct_token_config_endpoint(meta_ads_env, monkeypatch):
    session_factory = meta_ads_env["session_factory"]
    owner = meta_ads_env["owner"]
    org_a = meta_ads_env["org_a"]
    conn_a = meta_ads_env["conn_a"]

    def override_db():
        with session_factory() as s:
            yield s

    def auth_owner():
        return AuthenticatedPrincipal(
            uid=owner.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=owner,
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_owner

    monkeypatch.setattr(
        "app.api.meta_ads.list_accessible_ad_accounts",
        lambda access_token: [
            {
                "ad_account_id": "99887766",
                "name": "Direct Meta Brand",
                "currency": "USD",
                "account_status": 1,
                "timezone_name": "America/New_York",
            }
        ],
    )
    monkeypatch.setattr(
        "app.api.meta_ads.validate_ad_account_access",
        lambda access_token, account_id: {
            "ad_account_id": clean_ad_account_id(account_id),
            "name": "Direct Meta Brand",
            "currency": "USD",
            "account_status": 1,
            "timezone_name": "America/New_York",
        },
    )

    client = TestClient(app)

    # 1. Save direct access token without preselecting account -> needs_account_selection
    res1 = client.post(
        f"/api/v1/connections/{conn_a.id}/meta-ads/config",
        headers={"X-Organization-ID": str(org_a.id)},
        json={"access_token": "EAA_direct_secret_token"},
    )
    assert res1.status_code == 200, res1.text
    assert res1.json()["status"] == "needs_account_selection"
    assert res1.json()["has_secret"] is True
    assert "EAA_direct_secret_token" not in res1.text

    # 2. Save direct access token WITH ad_account_id -> connected
    res2 = client.post(
        f"/api/v1/connections/{conn_a.id}/meta-ads/config",
        headers={"X-Organization-ID": str(org_a.id)},
        json={"access_token": "EAA_direct_secret_token", "ad_account_id": "act_99887766"},
    )
    assert res2.status_code == 200, res2.text
    data2 = res2.json()
    assert data2["status"] == "connected"
    assert data2["ad_account_id"] == "99887766"
    assert data2["accounts_count"] == 1

    with session_factory() as db:
        saved = db.get(IntegrationConnection, conn_a.id)
        assert saved.config_json["currency"] == "USD"
        assert decrypt_secret(saved.config_json["access_token_encrypted"]) == "EAA_direct_secret_token"
