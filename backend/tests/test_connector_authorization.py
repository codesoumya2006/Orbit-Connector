from datetime import datetime, timezone
from uuid import UUID, uuid4
from unittest.mock import patch, AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cryptography.fernet import Fernet

from app.main import app
from app.core.config import settings
from app.core.database import Base, get_db
from app.models.entities import (
    CustomConnector,
    IntegrationConnection,
    Organization,
    OrganizationMembership,
    User,
)


@pytest.fixture
def connector_test_env(monkeypatch):
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
            firebase_uid="uid-owner",
            email="owner@orbit.test",
            email_verified=True,
        )
        admin_user = User(
            id=uuid4(),
            firebase_uid="uid-admin",
            email="admin@orbit.test",
            email_verified=True,
        )
        viewer_user = User(
            id=uuid4(),
            firebase_uid="uid-viewer",
            email="viewer@orbit.test",
            email_verified=True,
        )
        outsider_user = User(
            id=uuid4(),
            firebase_uid="uid-outsider",
            email="outsider@orbit.test",
            email_verified=True,
        )
        db.add_all([owner_user, admin_user, viewer_user, outsider_user])
        db.flush()

        org_a = Organization(id=uuid4(), name="Org Alpha")
        org_b = Organization(id=uuid4(), name="Org Beta")
        db.add_all([org_a, org_b])
        db.flush()

        # Memberships in Org A
        db.add(OrganizationMembership(user_id=owner_user.id, organization_id=org_a.id, role="owner"))
        db.add(OrganizationMembership(user_id=admin_user.id, organization_id=org_a.id, role="admin"))
        db.add(OrganizationMembership(user_id=viewer_user.id, organization_id=org_a.id, role="viewer"))

        # Outsider is only owner in Org B
        db.add(OrganizationMembership(user_id=outsider_user.id, organization_id=org_b.id, role="owner"))

        # Seed an existing connection and custom connector in Org B (tenant isolation test target)
        conn_b = IntegrationConnection(
            id="conn-beta-123",
            organization_id=org_b.id,
            provider_key="stripe",
            display_name="Org Beta Stripe",
            status="active",
            connection_type="oauth",
            config_json={"api_key": "secret_beta_stripe"},
        )
        connector_b = CustomConnector(
            id="connector-beta-456",
            organization_id=org_b.id,
            name="Org Beta Webhook",
            base_url="https://api.beta.test",
            auth_type="bearer",
            secret_encrypted=b"secret_beta_encrypted",
            endpoint_path="/events",
        )
        db.add_all([conn_b, connector_b])
        db.commit()

        org_a_id = org_a.id
        org_b_id = org_b.id
        owner_id = owner_user.id
        admin_id = admin_user.id
        viewer_id = viewer_user.id
        outsider_id = outsider_user.id

    from app.core.firebase_auth import AuthenticatedPrincipal, get_current_principal

    current_principal_state = {
        "user_id": owner_id,
        "email": "owner@orbit.test",
        "uid": "uid-owner",
    }

    def override_get_db():
        with session_factory() as db:
            yield db

    def override_get_principal():
        return AuthenticatedPrincipal(
            uid=current_principal_state["uid"],
            auth_time=datetime.now(timezone.utc),
            user=User(
                id=current_principal_state["user_id"],
                firebase_uid=current_principal_state["uid"],
                email=current_principal_state["email"],
                email_verified=True,
            ),
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_principal] = override_get_principal

    with TestClient(app, raise_server_exceptions=False) as client:
        yield {
            "client": client,
            "session_factory": session_factory,
            "org_a_id": org_a_id,
            "org_b_id": org_b_id,
            "owner_id": owner_id,
            "admin_id": admin_id,
            "viewer_id": viewer_id,
            "outsider_id": outsider_id,
            "current_principal_state": current_principal_state,
        }

    app.dependency_overrides.clear()
    engine.dispose()


def test_production_route_prefix_and_query_selector(connector_test_env):
    client = connector_test_env["client"]
    org_a = str(connector_test_env["org_a_id"])

    # Query selector works on /api/v1/connections
    res_conn = client.get(f"/api/v1/connections?organization_id={org_a}")
    assert res_conn.status_code == 200
    assert isinstance(res_conn.json(), list)

    # Query selector works on /api/v1/custom-connectors
    res_custom = client.get(f"/api/v1/custom-connectors?organization_id={org_a}")
    assert res_custom.status_code == 200
    assert isinstance(res_custom.json(), list)

    # Query selector fails on POST /api/v1/connections (only GET supported)
    res_post_query = client.post(
        f"/api/v1/connections?organization_id={org_a}",
        json={"provider_key": "stripe", "display_name": "Stripe Prod"},
    )
    assert res_post_query.status_code == 400
    assert "Query parameter 'organization_id' is only supported" in res_post_query.json()["detail"]


def test_owner_and_admin_connection_operations(connector_test_env):
    client = connector_test_env["client"]
    org_a = str(connector_test_env["org_a_id"])

    # 1. Owner creates connection in Org A
    res_owner_create = client.post(
        "/api/v1/connections",
        headers={"X-Organization-ID": org_a},
        json={
            "organization_id": org_a,
            "provider_key": "google_analytics",
            "display_name": "Google Analytics Workspace",
            "connection_type": "oauth",
        },
    )
    assert res_owner_create.status_code == 201
    conn_id = res_owner_create.json()["id"]

    # 2. Admin creates connection in Org A
    connector_test_env["current_principal_state"].update(
        {
            "user_id": connector_test_env["admin_id"],
            "email": "admin@orbit.test",
            "uid": "uid-admin",
        }
    )
    res_admin_create = client.post(
        "/api/v1/connections",
        headers={"X-Organization-ID": org_a},
        json={
            "provider_key": "google_ads",
            "display_name": "Google Ads Campaigns",
            "connection_type": "oauth",
        },
    )
    assert res_admin_create.status_code == 201
    admin_conn_id = res_admin_create.json()["id"]

    # 3. List connections in Org A as Admin
    res_list = client.get(
        "/api/v1/connections",
        headers={"X-Organization-ID": org_a},
    )
    assert res_list.status_code == 200
    returned_ids = {c["id"] for c in res_list.json()}
    assert conn_id in returned_ids
    assert admin_conn_id in returned_ids

    # 4. Admin deletes connection
    res_admin_del = client.delete(
        f"/api/v1/connections/{admin_conn_id}",
        headers={"X-Organization-ID": org_a},
    )
    assert res_admin_del.status_code == 204


def test_viewer_can_read_but_cannot_mutate_connections(connector_test_env):
    client = connector_test_env["client"]
    org_a = str(connector_test_env["org_a_id"])

    # Switch principal to viewer
    connector_test_env["current_principal_state"].update(
        {
            "user_id": connector_test_env["viewer_id"],
            "email": "viewer@orbit.test",
            "uid": "uid-viewer",
        }
    )

    # Viewer can read connections
    res_read = client.get(
        "/api/v1/connections",
        headers={"X-Organization-ID": org_a},
    )
    assert res_read.status_code == 200

    # Viewer cannot create connection -> 403
    res_write = client.post(
        "/api/v1/connections",
        headers={"X-Organization-ID": org_a},
        json={"provider_key": "hubspot", "display_name": "Hubspot CRM"},
    )
    assert res_write.status_code == 403
    assert "Permission 'connector:write' denied for role 'viewer'" in res_write.json()["detail"]

    # Viewer cannot delete connection -> 403
    res_del = client.delete(
        "/api/v1/connections/any-conn-id",
        headers={"X-Organization-ID": org_a},
    )
    assert res_del.status_code == 403


def test_custom_connector_authorization_and_secret_redaction(connector_test_env):
    client = connector_test_env["client"]
    org_a = str(connector_test_env["org_a_id"])

    # 1. Owner creates custom connector with secret
    secret_value = "super-secret-api-token-98765"
    res_create = client.post(
        "/api/v1/custom-connectors",
        headers={"X-Organization-ID": org_a},
        json={
            "name": "Inventory Webhook API",
            "base_url": "https://api.inventory.test",
            "endpoint_path": "/v1/items",
            "auth_type": "bearer",
            "secret": secret_value,
        },
    )
    assert res_create.status_code == 201
    custom_id = res_create.json()["id"]

    # Response does not leak secret
    assert secret_value not in res_create.text

    # 2. Viewer reads custom connectors -> metadata is visible, secret is NOT leaked
    connector_test_env["current_principal_state"].update(
        {
            "user_id": connector_test_env["viewer_id"],
            "email": "viewer@orbit.test",
            "uid": "uid-viewer",
        }
    )
    res_read = client.get(
        "/api/v1/custom-connectors",
        headers={"X-Organization-ID": org_a},
    )
    assert res_read.status_code == 200
    connectors = res_read.json()
    assert len(connectors) == 1
    c = connectors[0]
    assert c["id"] == custom_id
    assert c["name"] == "Inventory Webhook API"
    assert c["has_secret"] is True
    assert "secret_encrypted" not in c
    assert "secret" not in c
    assert secret_value not in res_read.text

    # 3. Viewer cannot create custom connector -> 403
    res_viewer_post = client.post(
        "/api/v1/custom-connectors",
        headers={"X-Organization-ID": org_a},
        json={
            "name": "Viewer Webhook",
            "base_url": "https://api.viewer.test",
            "auth_type": "none",
        },
    )
    assert res_viewer_post.status_code == 403

    # 4. Viewer cannot test custom connector -> 403
    res_viewer_test = client.post(
        "/api/v1/custom-connectors/test",
        headers={"X-Organization-ID": org_a},
        json={
            "base_url": "https://api.viewer.test",
            "auth_type": "none",
        },
    )
    assert res_viewer_test.status_code == 403


def test_cross_tenant_resource_access_fails_closed_with_404(connector_test_env):
    client = connector_test_env["client"]
    org_a = str(connector_test_env["org_a_id"])

    # Attempting to delete Org B's connection from Org A's context returns 404
    res_conn_idor = client.delete(
        "/api/v1/connections/conn-beta-123",
        headers={"X-Organization-ID": org_a},
    )
    assert res_conn_idor.status_code == 404
    assert res_conn_idor.json()["detail"] == "Connection not found."

    # Attempting to delete Org B's custom connector from Org A's context returns 404
    res_connector_idor = client.delete(
        "/api/v1/custom-connectors/connector-beta-456",
        headers={"X-Organization-ID": org_a},
    )
    assert res_connector_idor.status_code == 404
    assert res_connector_idor.json()["detail"] == "Custom connector not found."


def test_request_body_organization_id_mismatch_rejected(connector_test_env):
    client = connector_test_env["client"]
    org_a = str(connector_test_env["org_a_id"])
    org_b = str(connector_test_env["org_b_id"])

    # Header is Org A, but body contains Org B -> 400
    res_conn_mismatch = client.post(
        "/api/v1/connections",
        headers={"X-Organization-ID": org_a},
        json={
            "organization_id": org_b,
            "provider_key": "stripe",
            "display_name": "Spoofed Org",
        },
    )
    assert res_conn_mismatch.status_code == 400
    assert "Organization ID in request body does not match" in res_conn_mismatch.json()["detail"]

    # Same for custom connector
    res_custom_mismatch = client.post(
        "/api/v1/custom-connectors",
        headers={"X-Organization-ID": org_a},
        json={
            "organization_id": org_b,
            "name": "Spoofed Connector",
            "base_url": "https://api.test.test",
            "auth_type": "none",
        },
    )
    assert res_custom_mismatch.status_code == 400
    assert "Organization ID in request body does not match" in res_custom_mismatch.json()["detail"]


@pytest.mark.asyncio
async def test_custom_connector_ssrf_rejections_and_test_execution(connector_test_env):
    client = connector_test_env["client"]
    org_a = str(connector_test_env["org_a_id"])

    # 1. Non-HTTPS rejected with 400
    res_http = client.post(
        "/api/v1/custom-connectors/test",
        headers={"X-Organization-ID": org_a},
        json={"base_url": "http://insecure-api.test", "auth_type": "none"},
    )
    assert res_http.status_code == 400
    assert "must use an https:// URL" in res_http.json()["detail"]

    # 2. Localhost rejected with 400
    res_local = client.post(
        "/api/v1/custom-connectors/test",
        headers={"X-Organization-ID": org_a},
        json={"base_url": "https://localhost", "auth_type": "none"},
    )
    assert res_local.status_code == 400
    assert "Private or local network hosts are not allowed" in res_local.json()["detail"]

    # 3. Valid test call invokes test_rest_endpoint and does not persist credentials
    with patch(
        "app.api.routes.test_rest_endpoint",
        new=AsyncMock(return_value={"success": True, "http_status": 200, "message": "OK"}),
    ):
        res_test_ok = client.post(
            "/api/v1/custom-connectors/test",
            headers={"X-Organization-ID": org_a},
            json={
                "base_url": "https://api.public-service.test",
                "auth_type": "bearer",
                "secret": "temp-test-token",
            },
        )
        assert res_test_ok.status_code == 200
        assert res_test_ok.json()["success"] is True

    # Confirm no credentials or records were persisted into the database
    with connector_test_env["session_factory"]() as db:
        saved_connectors = (
            db.query(CustomConnector)
            .filter_by(organization_id=connector_test_env["org_a_id"])
            .count()
        )
        assert saved_connectors == 0
