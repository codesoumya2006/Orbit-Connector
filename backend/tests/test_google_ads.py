from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
from app.core.security import decrypt_secret, encrypt_secret
from app.main import app
from app.models.entities import (
    AuditEvent,
    GoogleAdsCampaignReport,
    IntegrationConnection,
    OAuthState,
    Organization,
    OrganizationMembership,
    SyncJob,
    User,
)
from app.providers.google_ads.adapter import GoogleAdsAdapter
from app.providers.google_ads.constants import (
    GOOGLE_ADS_API_BASE_URL,
    GOOGLE_ADS_API_VERSION,
    GOOGLE_ADS_SCOPE,
)
from app.providers.google_ads.customer import (
    GoogleAdsCustomerError,
    clean_customer_id,
    discover_accessible_customers,
    get_customer_details,
    list_accessible_customers,
    list_client_accounts_for_manager,
    validate_customer_access,
)
from app.providers.google_ads.oauth import (
    GoogleAdsOAuthError,
    build_google_ads_authorization_url,
    exchange_authorization_code,
    generate_pkce,
    refresh_access_token,
)
from app.providers.google_ads.reports import (
    NormalizedCampaignRow,
    build_gaql_campaign_query,
    cost_micros_to_decimal,
    fetch_google_ads_campaign_report,
    resolve_report_dates,
)
from app.workers.local_worker import (
    PermanentSyncError,
    RetryableSyncError,
    SyncWorkerError,
    process_claimed_job,
)


@pytest.fixture
def google_ads_env(monkeypatch):
    fernet_key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "credential_encryption_key", fernet_key)
    monkeypatch.setattr(settings, "google_client_id", "test-ads-client-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_client_secret", "test-ads-client-secret")
    monkeypatch.setattr(settings, "google_ads_developer_token", "test-developer-token-secret")
    monkeypatch.setattr(settings, "google_ads_redirect_uri", "http://localhost:8000/api/v1/oauth/google-ads/callback")
    monkeypatch.setattr(settings, "google_ads_api_version", "v26")

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
            firebase_uid="uid-ads-owner",
            email="owner@orbit.test",
            email_verified=True,
        )
        viewer = User(
            id=uuid4(),
            firebase_uid="uid-ads-viewer",
            email="viewer@orbit.test",
            email_verified=True,
        )
        other_user = User(
            id=uuid4(),
            firebase_uid="uid-ads-other",
            email="other@orbit.test",
            email_verified=True,
        )
        db.add_all([owner, viewer, other_user])
        db.flush()

        org_a = Organization(id=uuid4(), name="Ads Org A")
        org_b = Organization(id=uuid4(), name="Ads Org B")
        db.add_all([org_a, org_b])
        db.flush()

        db.add(OrganizationMembership(user_id=owner.id, organization_id=org_a.id, role="owner"))
        db.add(OrganizationMembership(user_id=viewer.id, organization_id=org_a.id, role="viewer"))
        db.add(OrganizationMembership(user_id=other_user.id, organization_id=org_b.id, role="owner"))

        conn_a = IntegrationConnection(
            id="conn-ads-alpha",
            organization_id=org_a.id,
            provider_key="google_ads",
            display_name="Google Ads Alpha",
            status="needs_authorization",
            connection_type="oauth",
            config_json={},
        )
        conn_b = IntegrationConnection(
            id="conn-ads-beta",
            organization_id=org_b.id,
            provider_key="google_ads",
            display_name="Google Ads Beta",
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

    engine.dispose()


def test_google_ads_api_version_centralization():
    """Verify that Google Ads API version is centralized to v26 and config-driven."""
    assert GOOGLE_ADS_API_VERSION == "v26"
    assert GOOGLE_ADS_API_BASE_URL == "https://googleads.googleapis.com/v26"
    assert settings.google_ads_api_version == "v26"


def test_customer_id_normalization_and_validation():
    """Verify customer ID normalization (digits only, strip hyphens/prefixes) and validation."""
    assert clean_customer_id("customers/123-456-7890") == "1234567890"
    assert clean_customer_id(" 987-654-3210 ") == "9876543210"
    assert clean_customer_id("1234567890") == "1234567890"

    with pytest.raises(GoogleAdsCustomerError, match="Must contain numeric digits"):
        clean_customer_id("invalid-customer-id")


def test_manager_and_client_account_discovery_hierarchy():
    """Verify discovery of directly accessible accounts and linked client accounts beneath managers.

    Ensures that:
    1. Direct advertiser accounts are discovered without login-customer-id.
    2. Manager accounts (MCC) are discovered.
    3. Client accounts beneath managers are enumerated via customer_client query.
    4. login-customer-id is correctly assigned to client accounts for subsequent operations.
    """
    captured_requests = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append({
            "url": str(request.url),
            "headers": dict(request.headers),
        })

        # 1. ListAccessibleCustomers endpoint
        if request.url.path.endswith("/customers:listAccessibleCustomers"):
            return httpx.Response(
                200,
                json={"resourceNames": ["customers/1111111111", "customers/2222222222"]},
            )

        # 2. Details query for Manager MCC 1111111111
        if "customers/1111111111/googleAds:search" in request.url.path:
            body = request.read().decode("utf-8")
            if "customer_client" in body:
                # CustomerClient GAQL query results
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            # level 0: Manager itself
                            {
                                "customerClient": {
                                    "id": "1111111111",
                                    "descriptiveName": "Main Agency MCC",
                                    "manager": True,
                                    "level": 0,
                                    "currencyCode": "USD",
                                    "timeZone": "America/New_York",
                                }
                            },
                            # level 1: Child client account
                            {
                                "customerClient": {
                                    "id": "3333333333",
                                    "descriptiveName": "E-Commerce Client Store",
                                    "manager": False,
                                    "level": 1,
                                    "currencyCode": "EUR",
                                    "timeZone": "Europe/Paris",
                                }
                            },
                            # level 1: Sub-manager account
                            {
                                "customerClient": {
                                    "id": "4444444444",
                                    "descriptiveName": "Regional Sub-MCC",
                                    "manager": True,
                                    "level": 1,
                                    "currencyCode": "USD",
                                    "timeZone": "America/Chicago",
                                }
                            },
                        ],
                        "nextPageToken": None,
                    },
                )
            else:
                # Customer metadata query
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "customer": {
                                    "id": "1111111111",
                                    "descriptiveName": "Main Agency MCC",
                                    "currencyCode": "USD",
                                    "timeZone": "America/New_York",
                                    "manager": True,
                                }
                            }
                        ]
                    },
                )

        # 3. Details query for Direct Advertiser 2222222222
        if "customers/2222222222/googleAds:search" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "customer": {
                                "id": "2222222222",
                                "descriptiveName": "Direct Brand Account",
                                "currencyCode": "USD",
                                "timeZone": "America/Los_Angeles",
                                "manager": False,
                            }
                        }
                    ]
                },
            )

        return httpx.Response(404, json={"error": "Not Found"})

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))

    discovered = discover_accessible_customers(
        access_token="test-tok",
        developer_token="test-dev-token",
        client=mock_client,
    )

    # Verify discovered accounts
    by_id = {c["customer_id"]: c for c in discovered}
    assert "1111111111" in by_id  # Manager MCC
    assert by_id["1111111111"]["is_manager"] is True
    assert by_id["1111111111"]["descriptive_name"] == "Main Agency MCC"

    assert "2222222222" in by_id  # Direct advertiser
    assert by_id["2222222222"]["is_manager"] is False
    assert by_id["2222222222"]["login_customer_id"] is None

    assert "3333333333" in by_id  # Client under MCC
    assert by_id["3333333333"]["is_manager"] is False
    assert by_id["3333333333"]["descriptive_name"] == "E-Commerce Client Store"
    assert by_id["3333333333"]["login_customer_id"] == "1111111111"
    assert by_id["3333333333"]["currency_code"] == "EUR"

    assert "4444444444" in by_id  # Sub-manager under MCC
    assert by_id["4444444444"]["is_manager"] is True
    assert by_id["4444444444"]["login_customer_id"] == "1111111111"

    # Verify header discipline:
    # 1. listAccessibleCustomers has developer-token but NO login-customer-id
    list_req = next(r for r in captured_requests if "listAccessibleCustomers" in r["url"])
    assert "developer-token" in list_req["headers"]
    assert "login-customer-id" not in list_req["headers"]

    # 2. customer_client search on manager 1111111111 sends login-customer-id: 1111111111
    mcc_search_req = next(
        r for r in captured_requests if "customers/1111111111/googleAds:search" in r["url"] and "login-customer-id" in r["headers"]
    )
    assert mcc_search_req["headers"]["login-customer-id"] == "1111111111"


def test_validate_customer_access_rejects_unauthorized_manager():
    """Verify that validate_customer_access rejects arbitrary/unauthorized login-customer-id."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/customers:listAccessibleCustomers"):
            return httpx.Response(
                200,
                json={"resourceNames": ["customers/1111111111"]},
            )
        return httpx.Response(404, json={})

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))

    # Target client 3333333333 with unauthorized manager 9999999999
    with pytest.raises(GoogleAdsCustomerError, match="Unauthorized login_customer_id"):
        validate_customer_access(
            access_token="tok",
            developer_token="dev",
            target_customer_id="3333333333",
            target_login_customer_id="9999999999",  # not in accessible list
            client=mock_client,
        )


def test_initiate_google_ads_oauth_success_and_rbac(google_ads_env):
    env = google_ads_env
    sf = env["session_factory"]

    def override_db():
        with sf() as s:
            yield s

    def auth_owner():
        return AuthenticatedPrincipal(
            uid=env["owner"].firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=env["owner"],
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_owner

    client = TestClient(app)
    headers = {"X-Organization-Id": str(env["org_a"].id)}

    resp = client.post(
        f"/api/v1/oauth/google-ads/authorize?connection_id={env['conn_a'].id}",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "authorization_url" in data
    assert "state" in data
    assert data["connection_id"] == env["conn_a"].id
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fadwords" in data["authorization_url"]

    # Verify PKCE code_verifier was encrypted at rest in OAuthState
    with sf() as db:
        state_record = db.query(OAuthState).filter_by(state_token=data["state"]).first()
        assert state_record is not None
        assert state_record.code_verifier.startswith("gAAAAA")
        assert len(decrypt_secret(state_record.code_verifier)) > 30

    # Viewer role is denied
    def auth_viewer():
        return AuthenticatedPrincipal(
            uid=env["viewer"].firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=env["viewer"],
        )

    app.dependency_overrides[get_current_principal] = auth_viewer
    resp_viewer = client.post(
        f"/api/v1/oauth/google-ads/authorize?connection_id={env['conn_a'].id}",
        headers=headers,
    )
    assert resp_viewer.status_code == 403

    app.dependency_overrides.clear()


def test_google_ads_oauth_callback_atomic_single_use_and_encryption(google_ads_env, monkeypatch):
    env = google_ads_env
    sf = env["session_factory"]

    def override_db():
        with sf() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    now = datetime.now(timezone.utc)
    raw_verifier, challenge = generate_pkce()
    encrypted_verifier = encrypt_secret(raw_verifier)
    state_token = "state-token-ads-test-123"

    with sf() as db:
        oauth_state = OAuthState(
            state_token=state_token,
            code_verifier=encrypted_verifier,
            organization_id=env["org_a"].id,
            user_id=env["owner"].id,
            connection_id=env["conn_a"].id,
            expires_at=now + timedelta(minutes=10),
            used_at=None,
            created_at=now,
        )
        db.add(oauth_state)
        db.commit()

    captured_code_verifier = {}

    def mock_exchange(code: str, code_verifier: str, client=None):
        captured_code_verifier["verifier"] = code_verifier
        return {
            "access_token": "mock-ads-access-token",
            "refresh_token": "mock-ads-refresh-token-secret-xyz",
            "scope": GOOGLE_ADS_SCOPE,
            "expires_in": 3600,
            "token_type": "Bearer",
        }

    monkeypatch.setattr(
        "app.api.google_ads.exchange_authorization_code",
        mock_exchange,
    )

    resp = client.get(
        f"/api/v1/oauth/google-ads/callback?code=mock-auth-code&state={state_token}",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "google_ads_connected=true" in resp.headers["location"]
    assert captured_code_verifier["verifier"] == raw_verifier

    # Replay attack rejected
    resp_replay = client.get(
        f"/api/v1/oauth/google-ads/callback?code=mock-auth-code-2&state={state_token}",
        follow_redirects=False,
    )
    assert resp_replay.status_code == 400
    assert "already been consumed" in resp_replay.text

    app.dependency_overrides.clear()


def test_customer_discovery_and_selection_with_hierarchy(google_ads_env, monkeypatch):
    env = google_ads_env
    sf = env["session_factory"]

    with sf() as db:
        conn = db.query(IntegrationConnection).filter_by(id=env["conn_a"].id).first()
        conn.config_json = {
            "refresh_token_encrypted": encrypt_secret("stored-refresh-token"),
            "authorized_at": datetime.now(timezone.utc).isoformat(),
        }
        conn.status = "needs_customer_selection"
        db.commit()

    def override_db():
        with sf() as s:
            yield s

    def auth_viewer():
        return AuthenticatedPrincipal(
            uid=env["viewer"].firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=env["viewer"],
        )

    def auth_owner():
        return AuthenticatedPrincipal(
            uid=env["owner"].firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=env["owner"],
        )

    monkeypatch.setattr(
        "app.api.google_ads.refresh_access_token",
        lambda rt: "fresh-ads-access-token",
    )
    monkeypatch.setattr(
        "app.api.google_ads.discover_accessible_customers",
        lambda access_token, developer_token: [
            {
                "customer_id": "1111111111",
                "descriptive_name": "Main Agency MCC",
                "is_manager": True,
                "currency_code": "USD",
                "time_zone": "America/New_York",
                "login_customer_id": None,
            },
            {
                "customer_id": "3333333333",
                "descriptive_name": "E-Commerce Client Store",
                "is_manager": False,
                "currency_code": "EUR",
                "time_zone": "Europe/Paris",
                "login_customer_id": "1111111111",
            },
        ],
    )
    monkeypatch.setattr(
        "app.api.google_ads.validate_customer_access",
        lambda access_token, developer_token, target_customer_id, target_login_customer_id=None: {
            "customer_id": clean_customer_id(target_customer_id),
            "descriptive_name": "E-Commerce Client Store",
            "login_customer_id": "1111111111",
            "currency_code": "EUR",
            "time_zone": "Europe/Paris",
            "is_manager": False,
        },
    )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_viewer

    client = TestClient(app)
    headers = {"X-Organization-Id": str(env["org_a"].id)}

    # Viewer can list customers
    resp = client.get(
        f"/api/v1/connections/{env['conn_a'].id}/google-ads/customers",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    cust_data = resp.json()["customers"]
    assert len(cust_data) == 2
    assert cust_data[0]["customer_id"] == "1111111111"
    assert cust_data[0]["is_manager"] is True
    assert cust_data[1]["customer_id"] == "3333333333"
    assert cust_data[1]["login_customer_id"] == "1111111111"
    assert cust_data[1]["currency_code"] == "EUR"

    # Owner selects client account with manager login-customer-id
    app.dependency_overrides[get_current_principal] = auth_owner
    select_payload = {
        "customer_id": "3333333333",
        "customer_name": "E-Commerce Client Store",
        "login_customer_id": "1111111111",
    }
    resp_owner_select = client.post(
        f"/api/v1/connections/{env['conn_a'].id}/google-ads/customers/select",
        headers=headers,
        json=select_payload,
    )
    assert resp_owner_select.status_code == 200, resp_owner_select.text
    assert resp_owner_select.json()["customer_id"] == "3333333333"
    assert resp_owner_select.json()["login_customer_id"] == "1111111111"
    assert resp_owner_select.json()["currency_code"] == "EUR"

    with sf() as db:
        updated_conn = db.query(IntegrationConnection).filter_by(id=env["conn_a"].id).first()
        assert updated_conn.status == "connected"
        assert updated_conn.config_json["customer_id"] == "3333333333"
        assert updated_conn.config_json["login_customer_id"] == "1111111111"
        assert updated_conn.config_json["currency_code"] == "EUR"

    app.dependency_overrides.clear()


def test_gaql_query_builder_v26_and_requested_fields():
    """Verify that GAQL query includes all required fields against v26 API."""
    query = build_gaql_campaign_query(
        start_date_iso="2025-01-01",
        end_date_iso="2025-01-31",
        campaign_ids=["111", "222"],
        limit=500,
    )
    assert "SELECT segments.date, customer.id, campaign.id, campaign.name, campaign.status" in query
    assert "metrics.impressions, metrics.clicks, metrics.cost_micros" in query
    assert "metrics.conversions, metrics.conversions_value" in query
    assert "FROM campaign" in query
    assert "WHERE segments.date BETWEEN '2025-01-01' AND '2025-01-31'" in query
    assert "AND campaign.id IN ('111', '222')" in query
    assert "ORDER BY segments.date DESC" in query
    assert "LIMIT 500" in query


def test_fetch_google_ads_campaign_report_pagination_and_decimal_precision():
    """Verify multi-page pagination using nextPageToken and Decimal financial precision."""
    page1_response = {
        "results": [
            {
                "customer": {"id": "1234567890"},
                "campaign": {
                    "id": "101",
                    "name": "Search - Brand",
                    "status": "ENABLED",
                },
                "segments": {"date": "2025-01-15"},
                "metrics": {
                    "impressions": "10000",
                    "clicks": "500",
                    "costMicros": "12500000",  # 12.50
                    "conversions": "25.0",
                    "conversionsValue": "1250.0",
                },
            }
        ],
        "nextPageToken": "token-for-page-2",
    }

    page2_response = {
        "results": [
            {
                "customer": {"id": "1234567890"},
                "campaign": {
                    "id": "102",
                    "name": "Performance Max",
                    "status": "ENABLED",
                },
                "segments": {"date": "2025-01-16"},
                "metrics": {
                    "impressions": "8000",
                    "clicks": "300",
                    "costMicros": "7250000",  # 7.25
                    "conversions": "15.0",
                    "conversionsValue": "900.0",
                },
            }
        ],
        "nextPageToken": None,
    }

    called_page_tokens = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.read().decode("utf-8"))
        token = body.get("pageToken")
        called_page_tokens.append(token)
        if token == "token-for-page-2":
            return httpx.Response(200, json=page2_response)
        return httpx.Response(200, json=page1_response)

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))

    rows = fetch_google_ads_campaign_report(
        access_token="test-access-token",
        developer_token="test-dev-token",
        customer_id="123-456-7890",
        start_date="2025-01-01",
        end_date="2025-01-31",
        client=mock_client,
    )

    # 1. Verify pagination retrieved both pages
    assert len(called_page_tokens) == 2
    assert called_page_tokens[0] is None
    assert called_page_tokens[1] == "token-for-page-2"
    assert len(rows) == 2

    # 2. Verify row 1 exact Decimal precision
    row1 = rows[0]
    assert row1.cost_micros == 12500000
    assert row1.cost == 12.5
    assert cost_micros_to_decimal(row1.cost_micros) == Decimal("12.5000")

    # 3. Verify row 2 exact Decimal precision
    row2 = rows[1]
    assert row2.cost_micros == 7250000
    assert row2.cost == 7.25
    assert cost_micros_to_decimal(row2.cost_micros) == Decimal("7.2500")


def test_fetch_google_ads_campaign_report_empty_behavior():
    """Verify empty report returns empty list gracefully."""
    mock_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"results": [], "nextPageToken": None})
        )
    )

    rows = fetch_google_ads_campaign_report(
        access_token="tok",
        developer_token="dev",
        customer_id="1234567890",
        start_date="2025-01-01",
        end_date="2025-01-31",
        client=mock_client,
    )
    assert rows == []


def test_developer_token_access_level_error_classification():
    """Verify that DEVELOPER_TOKEN_NOT_APPROVED is explicitly classified as PermanentSyncError."""
    error_403_response = {
        "error": {
            "code": 403,
            "message": "The developer token is not approved for use with this account.",
            "status": "PERMISSION_DENIED",
            "details": [
                {
                    "errors": [
                        {
                            "errorCode": {
                                "authorizationError": "DEVELOPER_TOKEN_NOT_APPROVED"
                            },
                            "message": "Developer token not approved.",
                        }
                    ]
                }
            ],
        }
    }
    client_403 = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(403, json=error_403_response)
        )
    )
    with pytest.raises(PermanentSyncError, match="permanent developer token error"):
        fetch_google_ads_campaign_report(
            access_token="test-tok",
            developer_token="test-dev",
            customer_id="123",
            start_date="2025-01-01",
            end_date="2025-01-31",
            client=client_403,
        )


def test_google_ads_adapter_execution_and_idempotent_upsert(google_ads_env, monkeypatch):
    env = google_ads_env
    sf = env["session_factory"]

    with sf() as db:
        conn = db.query(IntegrationConnection).filter_by(id=env["conn_a"].id).first()
        conn.config_json = {
            "refresh_token_encrypted": encrypt_secret("ads-refresh-token-valid"),
            "customer_id": "1234567890",
            "customer_name": "Test Ads Customer",
            "currency_code": "USD",
        }
        conn.status = "connected"

        now = datetime.now(timezone.utc)
        job = SyncJob(
            id=uuid4(),
            organization_id=env["org_a"].id,
            connection_id=conn.id,
            status="running",
            attempt_count=1,
            max_attempts=3,
            lease_token="worker-ads-1",
            lease_expires_at=now + timedelta(minutes=5),
            metadata_json={"start_date": "2025-01-01", "end_date": "2025-01-10"},
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        db.commit()

    monkeypatch.setattr(
        "app.providers.google_ads.adapter.refresh_access_token",
        lambda rt, client=None: "mock-fresh-access-token",
    )

    sample_rows = [
        NormalizedCampaignRow(
            report_date="2025-01-05",
            customer_id="1234567890",
            campaign_id="camp-101",
            campaign_name="Campaign 101",
            campaign_status="ENABLED",
            impressions=1000,
            clicks=50,
            cost_micros=2000000,  # 2.0
            cost=cost_micros_to_decimal(2000000),
            conversions=5.0,
            conversions_value=100.0,
        ),
        NormalizedCampaignRow(
            report_date="2025-01-06",
            customer_id="1234567890",
            campaign_id="camp-102",
            campaign_name="Campaign 102",
            campaign_status="PAUSED",
            impressions=500,
            clicks=20,
            cost_micros=1000000,  # 1.0
            cost=cost_micros_to_decimal(1000000),
            conversions=1.0,
            conversions_value=20.0,
        ),
    ]

    monkeypatch.setattr(
        "app.providers.google_ads.adapter.fetch_google_ads_campaign_report",
        lambda **kwargs: sample_rows,
    )

    with sf() as db:
        adapter = GoogleAdsAdapter(db=db)
        conn = db.query(IntegrationConnection).filter_by(id=env["conn_a"].id).first()
        job = db.query(SyncJob).filter_by(organization_id=env["org_a"].id).first()

        res1 = adapter.execute(conn, job)
        db.commit()

        assert res1["rows_synced"] == 2
        assert res1["total_impressions"] == 1500
        assert res1["total_clicks"] == 70
        assert res1["total_cost_micros"] == 3000000
        assert Decimal(str(res1["total_cost"])) == Decimal("3.0")
        assert res1["currency_code"] == "USD"

        persisted_rows = (
            db.query(GoogleAdsCampaignReport)
            .filter_by(organization_id=env["org_a"].id, connection_id=conn.id)
            .all()
        )
        assert len(persisted_rows) == 2


def test_local_worker_google_ads_dispatch(google_ads_env, monkeypatch):
    env = google_ads_env
    sf = env["session_factory"]

    with sf() as db:
        conn = db.query(IntegrationConnection).filter_by(id=env["conn_a"].id).first()
        conn.config_json = {
            "refresh_token_encrypted": encrypt_secret("ads-rt"),
            "customer_id": "1234567890",
            "customer_name": "Test Ads",
            "currency_code": "USD",
        }
        conn.status = "connected"

        now = datetime.now(timezone.utc)
        job = SyncJob(
            id=uuid4(),
            organization_id=env["org_a"].id,
            connection_id=conn.id,
            status="running",
            attempt_count=1,
            max_attempts=3,
            lease_token="worker-ads-test",
            lease_expires_at=now + timedelta(minutes=5),
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    adapter_executed = {"called": False}

    class MockGoogleAdsAdapter:
        def __init__(self, db, client=None):
            self.db = db

        def execute(self, connection, job):
            adapter_executed["called"] = True
            return {"rows_synced": 42, "status": "completed"}

    monkeypatch.setattr(
        "app.providers.google_ads.adapter.GoogleAdsAdapter",
        MockGoogleAdsAdapter,
    )

    with sf() as db:
        job = db.query(SyncJob).filter_by(id=job_id).first()
        res_job = process_claimed_job(
            db=db,
            job=job,
            worker_id="worker-ads-test",
        )
        db.commit()

        assert res_job.status == "succeeded"
        assert adapter_executed["called"] is True
        assert res_job.result_json.get("rows_synced") == 42


def test_get_google_ads_reports_api_summary_and_currency_documenting(google_ads_env):
    """Verify reports endpoint documents account currency and calculates totals with Decimal precision."""
    env = google_ads_env
    sf = env["session_factory"]

    now = datetime.now(timezone.utc)
    with sf() as db:
        conn = db.query(IntegrationConnection).filter_by(id=env["conn_a"].id).first()
        conn.config_json = {
            "customer_id": "1234567890",
            "customer_name": "Euro Client",
            "currency_code": "EUR",
        }
        conn.status = "connected"

        r1 = GoogleAdsCampaignReport(
            id=uuid4(),
            organization_id=env["org_a"].id,
            connection_id=env["conn_a"].id,
            customer_id="1234567890",
            report_date="2025-01-10",
            campaign_id="camp-1",
            campaign_name="Brand Search",
            campaign_status="ENABLED",
            impressions=1000,
            clicks=100,
            cost_micros=5000000,  # 5.0 EUR
            cost=Decimal("5.000000"),
            conversions=10.0,
            conversions_value=100.0,
            created_at=now,
            updated_at=now,
        )
        r2 = GoogleAdsCampaignReport(
            id=uuid4(),
            organization_id=env["org_a"].id,
            connection_id=env["conn_a"].id,
            customer_id="1234567890",
            report_date="2025-01-15",
            campaign_id="camp-2",
            campaign_name="Display Network",
            campaign_status="ENABLED",
            impressions=2000,
            clicks=150,
            cost_micros=3250000,  # 3.25 EUR
            cost=Decimal("3.250000"),
            conversions=5.5,
            conversions_value=55.0,
            created_at=now,
            updated_at=now,
        )
        db.add_all([r1, r2])
        db.commit()

    def override_db():
        with sf() as s:
            yield s

    def auth_org_a_viewer():
        return AuthenticatedPrincipal(
            uid=env["viewer"].firebase_uid,
            auth_time=now,
            user=env["viewer"],
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_org_a_viewer

    client = TestClient(app)
    headers_org_a = {"X-Organization-Id": str(env["org_a"].id)}

    resp = client.get(
        f"/api/v1/connections/{env['conn_a'].id}/google-ads/reports",
        headers=headers_org_a,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert body["summary"]["currency_code"] == "EUR"
    assert body["summary"]["total_cost_micros"] == 8250000
    assert Decimal(str(body["summary"]["total_cost"])) == Decimal("8.25")
    assert body["summary"]["total_conversions"] == 15.5
    assert body["summary"]["total_conversions_value"] == 155.0

    # Cross-tenant access fails with 404
    def auth_org_b_user():
        return AuthenticatedPrincipal(
            uid=env["other_user"].firebase_uid,
            auth_time=now,
            user=env["other_user"],
        )

    app.dependency_overrides[get_current_principal] = auth_org_b_user
    headers_org_b = {"X-Organization-Id": str(env["org_b"].id)}

    resp_tenant_leak = client.get(
        f"/api/v1/connections/{env['conn_a'].id}/google-ads/reports",
        headers=headers_org_b,
    )
    assert resp_tenant_leak.status_code == 404

    app.dependency_overrides.clear()


def test_google_ads_disconnect_and_token_revocation(google_ads_env, monkeypatch):
    env = google_ads_env
    sf = env["session_factory"]

    with sf() as db:
        conn = db.query(IntegrationConnection).filter_by(id=env["conn_a"].id).first()
        conn.config_json = {
            "refresh_token_encrypted": encrypt_secret("active-refresh-token"),
            "customer_id": "1234567890",
            "customer_name": "Active Account",
        }
        conn.status = "connected"
        db.commit()

    def override_db():
        with sf() as s:
            yield s

    revocation_called = {"called": False}

    def mock_revoke(token: str, client=None):
        revocation_called["called"] = True
        return True

    monkeypatch.setattr("app.api.google_ads.revoke_token", mock_revoke)

    def auth_owner():
        return AuthenticatedPrincipal(
            uid=env["owner"].firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=env["owner"],
        )

    def auth_viewer():
        return AuthenticatedPrincipal(
            uid=env["viewer"].firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=env["viewer"],
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_viewer

    client = TestClient(app)
    headers = {"X-Organization-Id": str(env["org_a"].id)}

    # Viewer cannot disconnect
    resp_viewer = client.post(
        f"/api/v1/connections/{env['conn_a'].id}/google-ads/disconnect",
        headers=headers,
    )
    assert resp_viewer.status_code == 403

    # Owner can disconnect
    app.dependency_overrides[get_current_principal] = auth_owner
    resp_owner = client.post(
        f"/api/v1/connections/{env['conn_a'].id}/google-ads/disconnect",
        headers=headers,
    )
    assert resp_owner.status_code == 200, resp_owner.text
    assert resp_owner.json()["disconnected"] is True
    assert revocation_called["called"] is True

    with sf() as db:
        updated = db.query(IntegrationConnection).filter_by(id=env["conn_a"].id).first()
        assert updated.status == "disconnected"
        assert updated.config_json == {}

    app.dependency_overrides.clear()


# ============================================================================
# Phase 7.1.1 Tests: Monetary Precision, Customer Metadata GAQL, & Hierarchy
# ============================================================================


def test_cost_micros_to_decimal_precision():
    """Verify exact Decimal conversion of cost_micros without quantization loss."""
    # 1 micro becomes 0.000001
    one_micro = cost_micros_to_decimal(1)
    assert one_micro == Decimal("0.000001")
    assert str(one_micro) == "0.000001"

    # 123456 micros becomes 0.123456
    sample_micros = cost_micros_to_decimal(123456)
    assert sample_micros == Decimal("0.123456")
    assert str(sample_micros) == "0.123456"

    # Large integer micros values remain exact without floating point drift
    large_micros = 987654321098765
    large_dec = cost_micros_to_decimal(large_micros)
    assert large_dec == Decimal("987654321.098765")
    assert str(large_dec) == "987654321.098765"


def test_summing_campaign_rows_exact_decimal():
    """Verify summing multiple rows with fractional micros produces exact Decimal."""
    micros_values = [1, 123456, 999999, 5000000]
    # Summing integers
    total_micros = sum(micros_values)
    assert total_micros == 6123456

    # Converting sum
    total_cost_dec = cost_micros_to_decimal(total_micros)
    assert total_cost_dec == Decimal("6.123456")

    # Summing individual decimals matches exactly
    individual_decimals = [cost_micros_to_decimal(m) for m in micros_values]
    assert sum(individual_decimals) == Decimal("6.123456")


def test_database_roundtrip_cost_numeric_precision(google_ads_env):
    """Verify SQLAlchemy Numeric(20, 6) preserves Decimal precision on database round-trip."""
    env = google_ads_env
    sf = env["session_factory"]

    test_cost_dec = Decimal("123.456789")
    report_id = uuid4()
    now = datetime.now(timezone.utc)

    with sf() as db:
        row = GoogleAdsCampaignReport(
            id=report_id,
            organization_id=env["org_a"].id,
            connection_id=env["conn_a"].id,
            customer_id="1234567890",
            report_date="2025-02-01",
            campaign_id="camp-precision",
            campaign_name="Precision Campaign",
            campaign_status="ENABLED",
            impressions=100,
            clicks=10,
            cost_micros=123456789,
            cost=test_cost_dec,
            conversions=1.0,
            conversions_value=10.0,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.commit()

    with sf() as db:
        loaded = db.query(GoogleAdsCampaignReport).filter_by(id=report_id).first()
        assert loaded is not None
        assert isinstance(loaded.cost, Decimal)
        assert loaded.cost == Decimal("123.456789")
        assert loaded.cost_micros == 123456789


def test_api_serialization_no_float_conversion():
    """Verify Pydantic schemas and jsonable_encoder serialize Decimal without float conversion."""
    from fastapi.encoders import jsonable_encoder
    from app.schemas.google_ads import GoogleAdsCampaignReportRowSchema, GoogleAdsReportSummary

    exact_cost = Decimal("0.123456")
    now = datetime.now(timezone.utc)

    row_schema = GoogleAdsCampaignReportRowSchema(
        id=uuid4(),
        organization_id=uuid4(),
        connection_id="conn-1",
        customer_id="1234567890",
        report_date="2025-01-01",
        campaign_id="camp-1",
        campaign_name="Test Campaign",
        campaign_status="ENABLED",
        impressions=100,
        clicks=5,
        cost_micros=123456,
        cost=exact_cost,
        conversions=1.0,
        conversions_value=10.0,
        created_at=now,
        updated_at=now,
    )

    # In Python schema, cost is a Decimal
    assert isinstance(row_schema.cost, Decimal)
    assert row_schema.cost == Decimal("0.123456")

    # In jsonable_encoder (FastAPI response serialization), cost is serialized as str, not float
    encoded = jsonable_encoder(row_schema)
    assert isinstance(encoded["cost"], str)
    assert encoded["cost"] == "0.123456"

    # Summary schema
    summary_schema = GoogleAdsReportSummary(
        currency_code="USD",
        total_impressions=100,
        total_clicks=5,
        total_cost_micros=123456,
        total_cost=exact_cost,
    )
    assert isinstance(summary_schema.total_cost, Decimal)
    encoded_summary = jsonable_encoder(summary_schema)
    assert isinstance(encoded_summary["total_cost"], str)
    assert encoded_summary["total_cost"] == "0.123456"


def test_customer_metadata_lookup_method_url_headers_gaql_and_parsing():
    """Verify customer metadata lookup asserts exact POST method, search URL, headers, GAQL, and parsing."""
    captured = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        body = request.read().decode("utf-8")
        captured["body"] = body

        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "customer": {
                            "id": "9998887776",
                            "descriptiveName": "Acme Global Ads",
                            "currencyCode": "EUR",
                            "timeZone": "Europe/Berlin",
                            "manager": False,
                            "testAccount": True,
                        }
                    }
                ]
            },
        )

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))

    details = get_customer_details(
        access_token="valid-tok",
        developer_token="valid-dev-token",
        customer_id="999-888-7776",
        login_customer_id="111-222-3333",
        client=mock_client,
    )

    # Assert exact HTTP method: POST (not GET)
    assert captured["method"] == "POST"

    # Assert URL uses GoogleAdsService search on customer endpoint
    assert captured["url"] == f"{GOOGLE_ADS_API_BASE_URL}/customers/9998887776/googleAds:search"

    # Assert headers
    assert captured["headers"]["authorization"] == "Bearer valid-tok"
    assert captured["headers"]["developer-token"] == "valid-dev-token"
    assert captured["headers"]["login-customer-id"] == "1112223333"
    assert "application/json" in captured["headers"]["content-type"]

    # Assert GAQL query selects all 6 required fields
    body_text = captured["body"]
    assert "customer.id" in body_text
    assert "customer.descriptive_name" in body_text
    assert "customer.currency_code" in body_text
    assert "customer.time_zone" in body_text
    assert "customer.manager" in body_text
    assert "customer.test_account" in body_text
    assert "FROM customer" in body_text
    assert "LIMIT 1" in body_text

    # Assert parsed customer attributes
    assert details["customer_id"] == "9998887776"
    assert details["descriptive_name"] == "Acme Global Ads"
    assert details["currency_code"] == "EUR"
    assert details["time_zone"] == "Europe/Berlin"
    assert details["is_manager"] is False
    assert details["test_account"] is True
    assert details["login_customer_id"] == "1112223333"


def test_customer_metadata_lookup_error_handling():
    """Verify get_customer_details error handling on HTTP non-200, empty results, and network error."""
    # 1. 403 Forbidden error response
    def mock_403(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"message": "User does not have access to customer 12345."}},
        )

    client_403 = httpx.Client(transport=httpx.MockTransport(mock_403))
    with pytest.raises(GoogleAdsCustomerError, match="Failed to query customer details"):
        get_customer_details(
            access_token="tok",
            developer_token="dev",
            customer_id="12345",
            client=client_403,
        )

    # 2. Empty results array response
    def mock_empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    client_empty = httpx.Client(transport=httpx.MockTransport(mock_empty))
    with pytest.raises(GoogleAdsCustomerError, match="returned no customer metadata records"):
        get_customer_details(
            access_token="tok",
            developer_token="dev",
            customer_id="12345",
            client=client_empty,
        )

    # 3. Network error
    def mock_net_err(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    client_net = httpx.Client(transport=httpx.MockTransport(mock_net_err))
    with pytest.raises(GoogleAdsCustomerError, match="Network error querying customer details"):
        get_customer_details(
            access_token="tok",
            developer_token="dev",
            customer_id="12345",
            client=client_net,
        )


def test_manager_hierarchy_direct_advertiser():
    """Verify direct advertiser account discovery (no login_customer_id)."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/customers:listAccessibleCustomers"):
            return httpx.Response(200, json={"resourceNames": ["customers/1111111111"]})
        if "customers/1111111111/googleAds:search" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "customer": {
                                "id": "1111111111",
                                "descriptiveName": "Direct Brand",
                                "currencyCode": "USD",
                                "timeZone": "America/New_York",
                                "manager": False,
                                "testAccount": False,
                            }
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    discovered = discover_accessible_customers("tok", "dev", client=client)

    assert len(discovered) == 1
    assert discovered[0]["customer_id"] == "1111111111"
    assert discovered[0]["is_manager"] is False
    assert discovered[0]["login_customer_id"] is None


def test_manager_hierarchy_direct_mcc_child():
    """Verify discovery of direct MCC child at level 1 with login_customer_id assigned."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/customers:listAccessibleCustomers"):
            return httpx.Response(200, json={"resourceNames": ["customers/1000000000"]})
        if "customers/1000000000/googleAds:search" in request.url.path:
            body = request.read().decode("utf-8")
            if "customer_client" in body:
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "customerClient": {
                                    "id": "2000000000",
                                    "descriptiveName": "Level 1 Child Store",
                                    "currencyCode": "EUR",
                                    "timeZone": "Europe/Berlin",
                                    "manager": False,
                                    "status": "ENABLED",
                                    "level": 1,
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "customer": {
                                "id": "1000000000",
                                "descriptiveName": "Parent MCC",
                                "currencyCode": "USD",
                                "timeZone": "America/New_York",
                                "manager": True,
                                "testAccount": False,
                            }
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    discovered = discover_accessible_customers("tok", "dev", client=client)

    by_id = {c["customer_id"]: c for c in discovered}
    assert "1000000000" in by_id
    assert by_id["1000000000"]["is_manager"] is True
    assert by_id["1000000000"]["login_customer_id"] is None

    assert "2000000000" in by_id
    assert by_id["2000000000"]["is_manager"] is False
    assert by_id["2000000000"]["login_customer_id"] == "1000000000"


def test_manager_hierarchy_nested_mcc_and_deeper_advertiser():
    """Verify discovery includes nested MCC and deeper-level advertiser (level >= 2)."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/customers:listAccessibleCustomers"):
            return httpx.Response(200, json={"resourceNames": ["customers/1000000000"]})
        if "customers/1000000000/googleAds:search" in request.url.path:
            body = request.read().decode("utf-8")
            if "customer_client" in body:
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            # level 1: Nested sub-manager
                            {
                                "customerClient": {
                                    "id": "2000000000",
                                    "descriptiveName": "Regional Sub-MCC",
                                    "currencyCode": "USD",
                                    "timeZone": "America/Chicago",
                                    "manager": True,
                                    "status": "ENABLED",
                                    "level": 1,
                                }
                            },
                            # level 2: Deeper advertiser account under sub-manager
                            {
                                "customerClient": {
                                    "id": "3000000000",
                                    "descriptiveName": "Deep Advertiser Account",
                                    "currencyCode": "CAD",
                                    "timeZone": "America/Toronto",
                                    "manager": False,
                                    "status": "ENABLED",
                                    "level": 2,
                                }
                            },
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "customer": {
                                "id": "1000000000",
                                "descriptiveName": "Top MCC",
                                "currencyCode": "USD",
                                "timeZone": "America/New_York",
                                "manager": True,
                                "testAccount": False,
                            }
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    discovered = discover_accessible_customers("tok", "dev", client=client)

    by_id = {c["customer_id"]: c for c in discovered}
    assert "2000000000" in by_id
    assert by_id["2000000000"]["is_manager"] is True
    assert by_id["2000000000"]["login_customer_id"] == "1000000000"

    assert "3000000000" in by_id
    assert by_id["3000000000"]["is_manager"] is False
    assert by_id["3000000000"]["login_customer_id"] == "1000000000"
    assert by_id["3000000000"]["level"] == 2


def test_manager_hierarchy_duplicate_access_paths():
    """Verify deduplication preserves direct access relationship if account is also present under MCC."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/customers:listAccessibleCustomers"):
            # Account 2000000000 is directly accessible AND beneath manager 1000000000
            return httpx.Response(
                200,
                json={"resourceNames": ["customers/1000000000", "customers/2000000000"]},
            )
        if "customers/1000000000/googleAds:search" in request.url.path:
            body = request.read().decode("utf-8")
            if "customer_client" in body:
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "customerClient": {
                                    "id": "2000000000",
                                    "descriptiveName": "Brand Direct And Linked",
                                    "currencyCode": "USD",
                                    "timeZone": "America/New_York",
                                    "manager": False,
                                    "status": "ENABLED",
                                    "level": 1,
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "customer": {
                                "id": "1000000000",
                                "descriptiveName": "Parent MCC",
                                "currencyCode": "USD",
                                "timeZone": "America/New_York",
                                "manager": True,
                            }
                        }
                    ]
                },
            )
        if "customers/2000000000/googleAds:search" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "customer": {
                                "id": "2000000000",
                                "descriptiveName": "Brand Direct And Linked",
                                "currencyCode": "USD",
                                "timeZone": "America/New_York",
                                "manager": False,
                            }
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    discovered = discover_accessible_customers("tok", "dev", client=client)

    by_id = {c["customer_id"]: c for c in discovered}
    assert "2000000000" in by_id
    # Direct access MUST be preserved (login_customer_id is None, no manager required)
    assert by_id["2000000000"]["login_customer_id"] is None


def test_manager_hierarchy_invalid_manager_customer_relationship():
    """Verify validate_customer_access rejects unauthorized or unlinked manager IDs."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/customers:listAccessibleCustomers"):
            return httpx.Response(200, json={"resourceNames": ["customers/1000000000"]})
        if "customers/9999999999" in request.url.path:
            return httpx.Response(403, json={"error": {"message": "Not authorized on manager"}})
        if "customers/1000000000/googleAds:search" in request.url.path:
            headers = request.headers
            if headers.get("login-customer-id") == "1000000000":
                return httpx.Response(
                    404,
                    json={"error": {"message": "Customer 5555555555 not found under manager 1000000000"}},
                )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))

    # Reject fabricated/unauthorized manager ID 9999999999
    with pytest.raises(GoogleAdsCustomerError, match="Unauthorized login_customer_id"):
        validate_customer_access(
            access_token="tok",
            developer_token="dev",
            target_customer_id="5555555555",
            target_login_customer_id="9999999999",
            client=client,
        )

    # Reject valid manager ID when target customer is not in its subtree
    with pytest.raises(GoogleAdsCustomerError, match="is not accessible under manager account"):
        validate_customer_access(
            access_token="tok",
            developer_token="dev",
            target_customer_id="5555555555",
            target_login_customer_id="1000000000",
            client=client,
        )


def test_manager_hierarchy_disabled_client_account():
    """Verify disabled client accounts (status != 'ENABLED') are excluded and rejected."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/customers:listAccessibleCustomers"):
            return httpx.Response(200, json={"resourceNames": ["customers/1000000000"]})
        if "customers/1000000000/googleAds:search" in request.url.path:
            body = request.read().decode("utf-8")
            if "customer_client" in body:
                # Returns only ENABLED accounts in customer_client query
                return httpx.Response(200, json={"results": []})
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "customer": {
                                "id": "1000000000",
                                "descriptiveName": "Parent MCC",
                                "manager": True,
                            }
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    discovered = discover_accessible_customers("tok", "dev", client=client)

    # Cancelled/disabled account 4444444444 was filtered out and not discovered
    ids = [c["customer_id"] for c in discovered]
    assert "4444444444" not in ids

    # Validation also rejects disabled account
    with pytest.raises(GoogleAdsCustomerError, match="is not accessible"):
        validate_customer_access(
            access_token="tok",
            developer_token="dev",
            target_customer_id="4444444444",
            client=client,
        )


def test_manager_hierarchy_correct_login_customer_id_selection():
    """Verify validate_customer_access returns correct login_customer_id for valid child account."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/customers:listAccessibleCustomers"):
            return httpx.Response(200, json={"resourceNames": ["customers/1000000000"]})
        if "customers/1000000000/googleAds:search" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "customer": {
                                "id": "1000000000",
                                "descriptiveName": "Agency MCC",
                                "manager": True,
                            }
                        }
                    ]
                },
            )
        if "customers/7777777777/googleAds:search" in request.url.path:
            assert request.headers.get("login-customer-id") == "1000000000"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "customer": {
                                "id": "7777777777",
                                "descriptiveName": "Verified Client Brand",
                                "currencyCode": "GBP",
                                "timeZone": "Europe/London",
                                "manager": False,
                                "testAccount": False,
                            }
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))

    verified = validate_customer_access(
        access_token="tok",
        developer_token="dev",
        target_customer_id="777-777-7777",
        target_login_customer_id="1000000000",
        client=client,
    )

    assert verified["customer_id"] == "7777777777"
    assert verified["descriptive_name"] == "Verified Client Brand"
    assert verified["currency_code"] == "GBP"
    assert verified["time_zone"] == "Europe/London"
    assert verified["is_manager"] is False
    assert verified["login_customer_id"] == "1000000000"


def test_google_ads_oauth_authorize_returns_503_without_orphan_state_when_unconfigured(
    google_ads_env, monkeypatch
):
    session_factory = google_ads_env["session_factory"]
    owner = google_ads_env["owner"]
    org_a = google_ads_env["org_a"]
    conn_a = google_ads_env["conn_a"]

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
    monkeypatch.setattr(settings, "google_client_id", "")

    try:
        client = TestClient(app)
        res = client.post(
            "/api/v1/oauth/google-ads/authorize",
            headers={"X-Organization-ID": str(org_a.id)},
            json={"connection_id": conn_a.id},
        )
        assert res.status_code == 503
        assert "GOOGLE_CLIENT_ID" in res.json()["detail"]

        with session_factory() as db:
            assert db.query(OAuthState).filter_by(connection_id=conn_a.id).all() == []
    finally:
        app.dependency_overrides.clear()


def test_google_ads_per_connection_developer_token_config_and_resolution(
    google_ads_env, monkeypatch
):
    session_factory = google_ads_env["session_factory"]
    owner = google_ads_env["owner"]
    org_a = google_ads_env["org_a"]
    conn_a = google_ads_env["conn_a"]

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
    monkeypatch.setattr(settings, "google_ads_developer_token", "")

    with session_factory() as db:
        conn = db.get(IntegrationConnection, conn_a.id)
        conn.config_json = {
            "access_token_encrypted": encrypt_secret("ya29.ads_access_tok"),
            "refresh_token_encrypted": encrypt_secret("1//ads_refresh_tok"),
            "token_expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        conn.status = "needs_customer_selection"
        db.commit()

    def fake_discover(access_token: str, developer_token: str):
        assert access_token == "ya29.ads_access_tok"
        assert developer_token == "per-conn-dev-tok-999"
        return [
            {
                "customer_id": "1234567890",
                "descriptive_name": "Client Brand",
                "currency_code": "USD",
                "time_zone": "America/New_York",
                "is_manager": False,
                "login_customer_id": "1234567890",
            }
        ]

    monkeypatch.setattr("app.api.google_ads.refresh_access_token", lambda rt: "ya29.ads_access_tok")
    monkeypatch.setattr("app.api.google_ads.discover_accessible_customers", fake_discover)

    try:
        client = TestClient(app)

        # Before developer token is stored anywhere -> /customers returns 400
        res_missing = client.get(
            f"/api/v1/connections/{conn_a.id}/google-ads/customers",
            headers={"X-Organization-ID": str(org_a.id)},
        )
        assert res_missing.status_code == 400
        assert "developer token" in res_missing.json()["detail"].lower()

        # Store per-connection developer token
        res_cfg = client.post(
            f"/api/v1/connections/{conn_a.id}/google-ads/config",
            headers={"X-Organization-ID": str(org_a.id)},
            json={"developer_token": "per-conn-dev-tok-999"},
        )
        assert res_cfg.status_code == 200, res_cfg.text
        assert res_cfg.json()["has_developer_token"] is True
        assert "per-conn-dev-tok-999" not in res_cfg.text

        # Verify encrypted at rest and exposed safely via /connections
        with session_factory() as db:
            updated = db.get(IntegrationConnection, conn_a.id)
            assert decrypt_secret(updated.config_json["developer_token_encrypted"]) == "per-conn-dev-tok-999"

        res_list = client.get("/api/v1/connections", headers={"X-Organization-ID": str(org_a.id)})
        assert res_list.status_code == 200
        row = next(c for c in res_list.json() if c["id"] == conn_a.id)
        assert row["has_developer_token"] is True
        assert "per-conn-dev-tok-999" not in res_list.text

        # Now /customers succeeds using the stored per-connection developer token
        res_customers = client.get(
            f"/api/v1/connections/{conn_a.id}/google-ads/customers",
            headers={"X-Organization-ID": str(org_a.id)},
        )
        assert res_customers.status_code == 200, res_customers.text
        assert res_customers.json()["customers"][0]["customer_id"] == "1234567890"
    finally:
        app.dependency_overrides.clear()
