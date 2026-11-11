from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

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
    User,
)
from app.providers.groq.client import (
    GroqAuthError,
    GroqModelError,
    GroqRateLimitError,
    GroqRequestError,
    execute_groq_completion,
    list_groq_models,
    validate_groq_api_key,
)
from app.providers.groq.constants import (
    DEFAULT_GROQ_MODEL,
    GROQ_API_BASE_URL,
    GROQ_CHAT_COMPLETIONS_ENDPOINT,
    GROQ_MODELS_ENDPOINT,
)

_real_httpx_client = httpx.Client


@pytest.fixture
def groq_env(monkeypatch):
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
            firebase_uid="uid-groq-owner",
            email="owner@groq.test",
            email_verified=True,
        )
        viewer = User(
            id=uuid4(),
            firebase_uid="uid-groq-viewer",
            email="viewer@groq.test",
            email_verified=True,
        )
        other_user = User(
            id=uuid4(),
            firebase_uid="uid-groq-other",
            email="other@groq.test",
            email_verified=True,
        )
        db.add_all([owner, viewer, other_user])
        db.flush()

        org_a = Organization(id=uuid4(), name="Groq Org A")
        org_b = Organization(id=uuid4(), name="Groq Org B")
        db.add_all([org_a, org_b])
        db.flush()

        db.add(OrganizationMembership(user_id=owner.id, organization_id=org_a.id, role="owner"))
        db.add(OrganizationMembership(user_id=viewer.id, organization_id=org_a.id, role="viewer"))
        db.add(OrganizationMembership(user_id=other_user.id, organization_id=org_b.id, role="owner"))

        conn_a = IntegrationConnection(
            id="conn-groq-alpha",
            organization_id=org_a.id,
            provider_key="groq",
            display_name="Groq Alpha",
            status="needs_configuration",
            connection_type="api_key",
            config_json={},
        )
        conn_b = IntegrationConnection(
            id="conn-groq-beta",
            organization_id=org_b.id,
            provider_key="groq",
            display_name="Groq Beta",
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


# ---------------------------------------------------------------------------
# Unit tests: Client methods
# ---------------------------------------------------------------------------


def test_validate_groq_api_key_success():
    def handler(request: httpx.Request):
        assert request.headers["Authorization"] == "Bearer gsk_valid_key"
        assert request.url.path == "/openai/v1/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "llama-3.3-70b-versatile", "owned_by": "meta"},
                    {"id": "llama-3.1-8b-instant", "owned_by": "meta"},
                ]
            },
        )

    with _real_httpx_client(transport=httpx.MockTransport(handler)) as client:
        models = validate_groq_api_key("gsk_valid_key", client=client)
        assert "llama-3.3-70b-versatile" in models
        assert "llama-3.1-8b-instant" in models


def test_validate_groq_api_key_auth_error():
    def handler(request: httpx.Request):
        return httpx.Response(
            401,
            json={"error": {"message": "Invalid API Key", "type": "invalid_request_error"}},
        )

    with _real_httpx_client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GroqAuthError) as exc_info:
            validate_groq_api_key("gsk_invalid", client=client)
        assert "Invalid or unauthorized" in str(exc_info.value)


def test_validate_groq_api_key_rate_limit():
    def handler(request: httpx.Request):
        return httpx.Response(
            429,
            json={"error": {"message": "Rate limit exceeded"}},
        )

    with _real_httpx_client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GroqRateLimitError):
            validate_groq_api_key("gsk_key", client=client)


def test_list_groq_models():
    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "llama-3.3-70b-versatile", "owned_by": "meta", "active": True},
                    {"id": "whisper-large-v3", "owned_by": "openai", "active": True},
                ]
            },
        )

    with _real_httpx_client(transport=httpx.MockTransport(handler)) as client:
        models = list_groq_models("gsk_key", client=client)
        assert len(models) >= 2
        ids = [m["id"] for m in models]
        assert "llama-3.3-70b-versatile" in ids
        assert "whisper-large-v3" in ids


def test_execute_groq_completion_success():
    captured_payload = {}

    def handler(request: httpx.Request):
        import json
        nonlocal captured_payload
        assert request.url.path == "/openai/v1/chat/completions"
        captured_payload = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test-123",
                "model": "llama-3.3-70b-versatile",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Hello from Groq LPU!"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 15,
                    "completion_tokens": 8,
                    "total_tokens": 23,
                },
            },
        )

    with _real_httpx_client(transport=httpx.MockTransport(handler)) as client:
        result = execute_groq_completion(
            api_key="gsk_key",
            prompt="Hello!",
            model="llama-3.3-70b-versatile",
            max_completion_tokens=512,
            client=client,
        )
        assert result["content"] == "Hello from Groq LPU!"
        assert result["model"] == "llama-3.3-70b-versatile"
        assert result["prompt_tokens"] == 15
        assert result["completion_tokens"] == 8
        assert result["total_tokens"] == 23
        assert captured_payload["max_completion_tokens"] == 512
        assert captured_payload["model"] == "llama-3.3-70b-versatile"


def test_execute_groq_completion_model_not_found():
    def handler(request: httpx.Request):
        return httpx.Response(
            404,
            json={"error": {"message": "Model 'nonexistent-model' not found"}},
        )

    with _real_httpx_client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GroqModelError):
            execute_groq_completion(
                api_key="gsk_key",
                prompt="Hi",
                model="nonexistent-model",
                client=client,
            )


# ---------------------------------------------------------------------------
# API tests: Endpoints, Auth, Encryption & RBAC
# ---------------------------------------------------------------------------


def test_groq_configure_success(groq_env):
    owner = groq_env["owner"]
    org_a = groq_env["org_a"]
    conn_a = groq_env["conn_a"]
    sf = groq_env["session_factory"]

    def override_db():
        with sf() as s:
            yield s

    def auth_owner():
        return AuthenticatedPrincipal(
            uid=owner.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=owner,
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_owner

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "llama-3.3-70b-versatile"},
                    {"id": "llama-3.1-8b-instant"},
                ]
            },
        )

    def mock_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _real_httpx_client(*args, **kwargs)

    with patch("app.providers.groq.client.httpx.Client", side_effect=mock_factory):
        client = TestClient(app)
        resp = client.post(
            f"/api/v1/connections/{conn_a.id}/groq/config",
            headers={"X-Organization-Id": str(org_a.id)},
            json={
                "api_key": "gsk_prod_secret_12345",
                "selected_model": "llama-3.3-70b-versatile",
                "temperature": 0.5,
                "max_tokens": 512,
            },
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "connected"
    assert data["has_secret"] is True
    assert data["selected_model"] == "llama-3.3-70b-versatile"

    # Verify at rest encryption in DB
    with sf() as db:
        conn = db.query(IntegrationConnection).filter_by(id=conn_a.id).one()
        assert conn.status == "connected"
        encrypted = conn.config_json["api_key_encrypted"]
        assert encrypted != "gsk_prod_secret_12345"
        assert decrypt_secret(encrypted) == "gsk_prod_secret_12345"
        assert conn.config_json["temperature"] == 0.5
        assert conn.config_json["max_tokens"] == 512

        # Verify audit event
        audit = (
            db.query(AuditEvent)
            .filter_by(organization_id=org_a.id, event_type="connector.configured")
            .first()
        )
        assert audit is not None
        assert audit.metadata_json["provider_key"] == "groq"
        assert "gsk_prod_secret_12345" not in str(audit.metadata_json)


def test_groq_configure_rbac_viewer_forbidden(groq_env):
    viewer = groq_env["viewer"]
    org_a = groq_env["org_a"]
    conn_a = groq_env["conn_a"]
    sf = groq_env["session_factory"]

    def override_db():
        with sf() as s:
            yield s

    def auth_viewer():
        return AuthenticatedPrincipal(
            uid=viewer.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=viewer,
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_viewer

    client = TestClient(app)
    resp = client.post(
        f"/api/v1/connections/{conn_a.id}/groq/config",
        headers={"X-Organization-Id": str(org_a.id)},
        json={"api_key": "gsk_test"},
    )
    assert resp.status_code == 403


def test_groq_configure_tenant_isolation(groq_env):
    other = groq_env["other_user"]
    org_b = groq_env["org_b"]
    conn_a = groq_env["conn_a"]
    sf = groq_env["session_factory"]

    def override_db():
        with sf() as s:
            yield s

    def auth_other():
        return AuthenticatedPrincipal(
            uid=other.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=other,
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_other

    client = TestClient(app)
    resp = client.post(
        f"/api/v1/connections/{conn_a.id}/groq/config",
        headers={"X-Organization-Id": str(org_b.id)},
        json={"api_key": "gsk_test"},
    )
    assert resp.status_code == 404


def test_groq_test_connection_endpoint(groq_env):
    owner = groq_env["owner"]
    org_a = groq_env["org_a"]
    conn_a = groq_env["conn_a"]
    sf = groq_env["session_factory"]

    def override_db():
        with sf() as s:
            yield s

    def auth_owner():
        return AuthenticatedPrincipal(
            uid=owner.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=owner,
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_owner

    # Store encrypted credentials first
    with sf() as db:
        conn = db.query(IntegrationConnection).filter_by(id=conn_a.id).one()
        conn.config_json = {
            "api_key_encrypted": encrypt_secret("gsk_valid_key"),
            "selected_model": "llama-3.3-70b-versatile",
        }
        conn.status = "connected"
        db.commit()

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={"data": [{"id": "llama-3.3-70b-versatile"}]},
        )

    def mock_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _real_httpx_client(*args, **kwargs)

    with patch("app.providers.groq.client.httpx.Client", side_effect=mock_factory):
        client = TestClient(app)
        resp = client.post(
            f"/api/v1/connections/{conn_a.id}/groq/test",
            headers={"X-Organization-Id": str(org_a.id)},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["valid"] is True
    assert data["selected_model"] == "llama-3.3-70b-versatile"


def test_groq_get_models_endpoint(groq_env):
    owner = groq_env["owner"]
    org_a = groq_env["org_a"]
    conn_a = groq_env["conn_a"]
    sf = groq_env["session_factory"]

    def override_db():
        with sf() as s:
            yield s

    def auth_owner():
        return AuthenticatedPrincipal(
            uid=owner.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=owner,
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_owner

    with sf() as db:
        conn = db.query(IntegrationConnection).filter_by(id=conn_a.id).one()
        conn.config_json = {"api_key_encrypted": encrypt_secret("gsk_key")}
        db.commit()

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "llama-3.3-70b-versatile", "owned_by": "meta"},
                    {"id": "mixtral-8x7b-32768", "owned_by": "mistralai"},
                ]
            },
        )

    def mock_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _real_httpx_client(*args, **kwargs)

    with patch("app.providers.groq.client.httpx.Client", side_effect=mock_factory):
        client = TestClient(app)
        resp = client.get(
            f"/api/v1/connections/{conn_a.id}/groq/models",
            headers={"X-Organization-Id": str(org_a.id)},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    model_ids = [m["id"] for m in data["models"]]
    assert "llama-3.3-70b-versatile" in model_ids
    assert "mixtral-8x7b-32768" in model_ids


def test_groq_select_model_endpoint(groq_env):
    owner = groq_env["owner"]
    org_a = groq_env["org_a"]
    conn_a = groq_env["conn_a"]
    sf = groq_env["session_factory"]

    def override_db():
        with sf() as s:
            yield s

    def auth_owner():
        return AuthenticatedPrincipal(
            uid=owner.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=owner,
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_owner

    client = TestClient(app)
    resp = client.post(
        f"/api/v1/connections/{conn_a.id}/groq/models/select",
        headers={"X-Organization-Id": str(org_a.id)},
        json={"model_id": "llama-3.1-8b-instant"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["selected_model"] == "llama-3.1-8b-instant"

    with sf() as db:
        conn = db.query(IntegrationConnection).filter_by(id=conn_a.id).one()
        assert conn.config_json["selected_model"] == "llama-3.1-8b-instant"


def test_groq_test_completion_endpoint_success(groq_env):
    owner = groq_env["owner"]
    org_a = groq_env["org_a"]
    conn_a = groq_env["conn_a"]
    sf = groq_env["session_factory"]

    def override_db():
        with sf() as s:
            yield s

    def auth_owner():
        return AuthenticatedPrincipal(
            uid=owner.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=owner,
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_owner

    with sf() as db:
        conn = db.query(IntegrationConnection).filter_by(id=conn_a.id).one()
        conn.config_json = {
            "api_key_encrypted": encrypt_secret("gsk_key"),
            "selected_model": "llama-3.3-70b-versatile",
        }
        db.commit()

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test-abc",
                "model": "llama-3.3-70b-versatile",
                "choices": [
                    {"message": {"role": "assistant", "content": "Orbit Connector Hub is ready!"}}
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 7,
                    "total_tokens": 19,
                },
            },
        )

    def mock_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _real_httpx_client(*args, **kwargs)

    with patch("app.providers.groq.client.httpx.Client", side_effect=mock_factory):
        client = TestClient(app)
        resp = client.post(
            f"/api/v1/connections/{conn_a.id}/groq/test-completion",
            headers={"X-Organization-Id": str(org_a.id)},
            json={"prompt": "Say hello to Orbit Hub"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["content"] == "Orbit Connector Hub is ready!"
    assert data["model"] == "llama-3.3-70b-versatile"
    assert data["total_tokens"] == 19


def test_groq_test_completion_rbac_viewer_forbidden(groq_env):
    viewer = groq_env["viewer"]
    org_a = groq_env["org_a"]
    conn_a = groq_env["conn_a"]
    sf = groq_env["session_factory"]

    def override_db():
        with sf() as s:
            yield s

    def auth_viewer():
        return AuthenticatedPrincipal(
            uid=viewer.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=viewer,
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_viewer

    client = TestClient(app)
    resp = client.post(
        f"/api/v1/connections/{conn_a.id}/groq/test-completion",
        headers={"X-Organization-Id": str(org_a.id)},
        json={"prompt": "Hello"},
    )
    assert resp.status_code == 403


def test_groq_disconnect_endpoint(groq_env):
    owner = groq_env["owner"]
    org_a = groq_env["org_a"]
    conn_a = groq_env["conn_a"]
    sf = groq_env["session_factory"]

    def override_db():
        with sf() as s:
            yield s

    def auth_owner():
        return AuthenticatedPrincipal(
            uid=owner.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=owner,
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_owner

    with sf() as db:
        conn = db.query(IntegrationConnection).filter_by(id=conn_a.id).one()
        conn.config_json = {
            "api_key_encrypted": encrypt_secret("gsk_key"),
            "selected_model": "llama-3.3-70b-versatile",
        }
        conn.status = "connected"
        db.commit()

    client = TestClient(app)
    resp = client.post(
        f"/api/v1/connections/{conn_a.id}/groq/disconnect",
        headers={"X-Organization-Id": str(org_a.id)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "disconnected"

    with sf() as db:
        conn = db.query(IntegrationConnection).filter_by(id=conn_a.id).one()
        assert conn.status == "disconnected"
        assert conn.config_json == {}

        # Audit event created
        audit = (
            db.query(AuditEvent)
            .filter_by(organization_id=org_a.id, event_type="connector.deleted")
            .first()
        )
        assert audit is not None
        assert audit.metadata_json["action"] == "disconnected"


def test_groq_credentials_redacted_on_connections_endpoint(groq_env):
    owner = groq_env["owner"]
    org_a = groq_env["org_a"]
    conn_a = groq_env["conn_a"]
    sf = groq_env["session_factory"]

    def override_db():
        with sf() as s:
            yield s

    def auth_owner():
        return AuthenticatedPrincipal(
            uid=owner.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=owner,
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_owner

    with sf() as db:
        conn = db.query(IntegrationConnection).filter_by(id=conn_a.id).one()
        conn.config_json = {
            "api_key_encrypted": encrypt_secret("gsk_secret_123"),
            "selected_model": "llama-3.3-70b-versatile",
        }
        conn.status = "connected"
        db.commit()

    client = TestClient(app)
    resp = client.get(
        "/api/v1/connections",
        headers={"X-Organization-Id": str(org_a.id)},
    )
    assert resp.status_code == 200, resp.text
    conns = resp.json()
    groq_conn = next(c for c in conns if c["id"] == conn_a.id)
    assert groq_conn["has_secret"] is True
    assert groq_conn["selected_model"] == "llama-3.3-70b-versatile"
    assert "api_key_encrypted" not in groq_conn
    assert "gsk_secret_123" not in str(resp.text)


def test_groq_configure_updates_model_and_params_without_reentering_api_key(groq_env):
    owner = groq_env["owner"]
    org_a = groq_env["org_a"]
    conn_a = groq_env["conn_a"]
    sf = groq_env["session_factory"]

    def override_db():
        with sf() as s:
            yield s

    def auth_owner():
        return AuthenticatedPrincipal(
            uid=owner.firebase_uid,
            auth_time=datetime.now(timezone.utc),
            user=owner,
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = auth_owner

    with sf() as db:
        conn = db.query(IntegrationConnection).filter_by(id=conn_a.id).one()
        conn.config_json = {
            "api_key_encrypted": encrypt_secret("gsk_existing_key_abc"),
            "selected_model": "llama-3.3-70b-versatile",
            "temperature": 0.7,
            "max_tokens": 1024,
        }
        conn.status = "connected"
        db.commit()

    def handler(request: httpx.Request):
        assert request.headers["authorization"] == "Bearer gsk_existing_key_abc"
        return httpx.Response(200, json={"data": [{"id": "llama-3.1-8b-instant"}]})

    def mock_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _real_httpx_client(*args, **kwargs)

    with patch("app.providers.groq.client.httpx.Client", side_effect=mock_factory):
        client = TestClient(app)
        resp = client.post(
            f"/api/v1/connections/{conn_a.id}/groq/config",
            headers={"X-Organization-Id": str(org_a.id)},
            json={
                "selected_model": "llama-3.1-8b-instant",
                "temperature": 0.25,
                "max_tokens": 2048,
            },
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["selected_model"] == "llama-3.1-8b-instant"

    with sf() as db:
        conn = db.query(IntegrationConnection).filter_by(id=conn_a.id).one()
        assert decrypt_secret(conn.config_json["api_key_encrypted"]) == "gsk_existing_key_abc"
        assert conn.config_json["selected_model"] == "llama-3.1-8b-instant"
        assert conn.config_json["temperature"] == 0.25
        assert conn.config_json["max_tokens"] == 2048
