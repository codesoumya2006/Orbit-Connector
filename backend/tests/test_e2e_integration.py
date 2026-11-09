from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base, get_db
from app.core.firebase_auth import AuthenticatedPrincipal, get_current_principal
from app.main import app
from app.models.entities import (
    AuditEvent,
    CustomConnector,
    IntegrationConnection,
    Organization,
    OrganizationMembership,
    User,
)


@pytest.fixture
def e2e_env(monkeypatch):
    monkeypatch.setattr(
        settings, "credential_encryption_key", Fernet.generate_key().decode()
    )
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # Active user context simulator
    active_principal = {
        "uid": "uid-owner-e2e",
        "email": "owner@orbit.e2e",
        "user_id": uuid4(),
        "role": "owner",
    }

    owner_user_id = active_principal["user_id"]
    # Pre-seed user in DB
    with session_factory() as db:
        user_row = User(
            id=owner_user_id,
            firebase_uid=active_principal["uid"],
            email=active_principal["email"],
            email_verified=True,
            display_name="E2E Owner",
        )
        db.add(user_row)
        db.commit()

    def override_get_db():
        with session_factory() as db:
            yield db

    def override_get_principal():
        return AuthenticatedPrincipal(
            uid=active_principal["uid"],
            auth_time=datetime.now(timezone.utc),
            user=User(
                id=active_principal["user_id"],
                firebase_uid=active_principal["uid"],
                email=active_principal["email"],
                email_verified=True,
                display_name="E2E User",
            ),
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_principal] = override_get_principal

    with TestClient(app, raise_server_exceptions=False) as client:
        yield {
            "client": client,
            "session_factory": session_factory,
            "engine": engine,
            "active_principal": active_principal,
            "owner_user_id": owner_user_id,
        }

    app.dependency_overrides.clear()
    engine.dispose()


def test_unauthenticated_requests_are_rejected():
    # Remove overrides to test raw unauthenticated endpoint rejection
    with TestClient(app, raise_server_exceptions=False) as raw_client:
        res_orgs = raw_client.get("/api/v1/organizations")
        assert res_orgs.status_code == 401

        res_conn = raw_client.get("/api/v1/connections")
        assert res_conn.status_code == 401

        res_custom = raw_client.get("/api/v1/custom-connectors")
        assert res_custom.status_code == 401


def test_complete_e2e_application_flow(e2e_env):
    client = e2e_env["client"]
    principal = e2e_env["active_principal"]
    session_factory = e2e_env["session_factory"]

    # 1. User has no organizations initially
    list_res = client.get("/api/v1/organizations")
    assert list_res.status_code == 200
    assert list_res.json() == []

    # 2. User creates an organization
    create_org_res = client.post(
        "/api/v1/organizations",
        json={"name": "Acme Analytics Corp"},
    )
    assert create_org_res.status_code == 201
    org_data = create_org_res.json()
    org_id = org_data["id"]
    assert org_data["name"] == "Acme Analytics Corp"
    assert org_data["role"] == "owner"

    # Verify audit event for organization creation
    with session_factory() as db:
        org_audit = (
            db.query(AuditEvent)
            .filter_by(
                organization_id=UUID(org_id),
                event_type="organization.created",
            )
            .one_or_none()
        )
        assert org_audit is not None
        assert org_audit.actor_user_id == principal["user_id"]
        assert org_audit.metadata_json == {"name": "Acme Analytics Corp"}

    # 3. User lists organizations -> now sees the new workspace
    list_res_2 = client.get("/api/v1/organizations")
    assert list_res_2.status_code == 200
    assert len(list_res_2.json()) == 1
    assert list_res_2.json()[0]["id"] == org_id
    assert list_res_2.json()[0]["role"] == "owner"

    # 4. User creates a provider connection in the organization
    conn_create_res = client.post(
        "/api/v1/connections",
        headers={"X-Organization-ID": org_id},
        json={
            "provider_key": "google_analytics",
            "display_name": "GA4 Marketing Production",
            "connection_type": "oauth",
        },
    )
    assert conn_create_res.status_code == 201
    conn_id = conn_create_res.json()["id"]

    # Verify connection audit event
    with session_factory() as db:
        conn_audit = (
            db.query(AuditEvent)
            .filter_by(
                organization_id=UUID(org_id),
                event_type="connection.created",
            )
            .one_or_none()
        )
        assert conn_audit is not None
        assert conn_audit.target_id == conn_id
        assert conn_audit.metadata_json["provider_key"] == "google_analytics"

    # 5. User creates a custom REST connector
    custom_res = client.post(
        "/api/v1/custom-connectors",
        headers={"X-Organization-ID": org_id},
        json={
            "name": "Acme Internal CRM",
            "base_url": "https://crm.acme.test",
            "endpoint_path": "/api/v2/leads",
            "auth_type": "bearer",
            "secret": "confidential_crm_token_987",
            "secret_label": "CRM API Key",
            "headers": {"X-Tenant": "acme"},
        },
    )
    assert custom_res.status_code == 201
    custom_id = custom_res.json()["id"]

    # Verify custom connector audit redaction
    with session_factory() as db:
        custom_audit = (
            db.query(AuditEvent)
            .filter_by(
                organization_id=UUID(org_id),
                event_type="custom_connector.created",
            )
            .one_or_none()
        )
        assert custom_audit is not None
        assert "confidential_crm_token_987" not in str(custom_audit.metadata_json)
        assert custom_audit.metadata_json["name"] == "Acme Internal CRM"

    # 6. Read connections and custom connectors
    list_conns = client.get(f"/api/v1/connections?organization_id={org_id}")
    assert list_conns.status_code == 200
    assert len(list_conns.json()) == 1
    assert list_conns.json()[0]["id"] == conn_id

    list_customs = client.get(f"/api/v1/custom-connectors?organization_id={org_id}")
    assert list_customs.status_code == 200
    assert len(list_customs.json()) == 1
    assert list_customs.json()[0]["id"] == custom_id
    assert list_customs.json()[0]["has_secret"] is True
    assert "confidential_crm_token_987" not in list_customs.text

    # 7. Add a second user as a Viewer
    viewer_user_id = uuid4()
    with session_factory() as db:
        viewer_user = User(
            id=viewer_user_id,
            firebase_uid="uid-viewer-e2e",
            email="viewer@orbit.e2e",
            email_verified=True,
            display_name="E2E Viewer",
        )
        db.add(viewer_user)
        db.add(
            OrganizationMembership(
                user_id=viewer_user_id,
                organization_id=UUID(org_id),
                role="viewer",
            )
        )
        db.commit()

    # Switch session to Viewer
    principal["uid"] = "uid-viewer-e2e"
    principal["email"] = "viewer@orbit.e2e"
    principal["user_id"] = viewer_user_id
    principal["role"] = "viewer"

    # Viewer can read connections and connectors
    v_conns = client.get(
        "/api/v1/connections",
        headers={"X-Organization-ID": org_id},
    )
    assert v_conns.status_code == 200
    assert len(v_conns.json()) == 1

    v_customs = client.get(
        "/api/v1/custom-connectors",
        headers={"X-Organization-ID": org_id},
    )
    assert v_customs.status_code == 200
    assert len(v_customs.json()) == 1

    # Viewer CANNOT mutate connections or connectors (RBAC 403)
    v_fail_conn = client.post(
        "/api/v1/connections",
        headers={"X-Organization-ID": org_id},
        json={
            "provider_key": "google_ads",
            "display_name": "Viewer Forbidden Ads",
        },
    )
    assert v_fail_conn.status_code == 403

    v_fail_del_conn = client.delete(
        f"/api/v1/connections/{conn_id}",
        headers={"X-Organization-ID": org_id},
    )
    assert v_fail_del_conn.status_code == 403

    v_fail_custom = client.post(
        "/api/v1/custom-connectors",
        headers={"X-Organization-ID": org_id},
        json={
            "name": "Viewer Forbidden Connector",
            "base_url": "https://api.forbidden.test",
        },
    )
    assert v_fail_custom.status_code == 403

    v_fail_del_custom = client.delete(
        f"/api/v1/custom-connectors/{custom_id}",
        headers={"X-Organization-ID": org_id},
    )
    assert v_fail_del_custom.status_code == 403

    # 8. Switch back to Owner
    principal["uid"] = "uid-owner-e2e"
    principal["email"] = "owner@orbit.e2e"
    principal["user_id"] = e2e_env["owner_user_id"]
    principal["role"] = "owner"

    # 9. Tenant Isolation: Create another organization for a competitor
    competitor_org_id = uuid4()
    with session_factory() as db:
        comp_org = Organization(id=competitor_org_id, name="Competitor Org")
        db.add(comp_org)
        comp_conn = IntegrationConnection(
            id="competitor-secret-conn",
            organization_id=competitor_org_id,
            provider_key="meta_ads",
            display_name="Competitor Meta",
            status="active",
            connection_type="oauth",
        )
        db.add(comp_conn)
        db.commit()

    # Owner attempts to access competitor connection -> 404 (anti-enumeration)
    cross_tenant_res = client.delete(
        "/api/v1/connections/competitor-secret-conn",
        headers={"X-Organization-ID": org_id},
    )
    assert cross_tenant_res.status_code == 404

    # 10. Owner deletes their own resources cleanly
    del_conn_res = client.delete(
        f"/api/v1/connections/{conn_id}",
        headers={"X-Organization-ID": org_id},
    )
    assert del_conn_res.status_code == 204

    del_custom_res = client.delete(
        f"/api/v1/custom-connectors/{custom_id}",
        headers={"X-Organization-ID": org_id},
    )
    assert del_custom_res.status_code == 204

    # 11. Explicit logout audit
    logout_res = client.post("/api/v1/auth/logout")
    assert logout_res.status_code == 204

    with session_factory() as db:
        logout_audit = (
            db.query(AuditEvent)
            .filter_by(event_type="auth.logout")
            .first()
        )
        assert logout_audit is not None
