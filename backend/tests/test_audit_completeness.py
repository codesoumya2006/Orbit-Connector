from datetime import datetime, timezone
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
    CustomConnector,
    IntegrationConnection,
    Organization,
    OrganizationMembership,
    User,
)


@pytest.fixture
def audit_test_env(monkeypatch):
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

    with session_factory() as db:
        owner_user = User(
            id=uuid4(),
            firebase_uid="uid-audit-owner",
            email="owner@orbit.test",
            email_verified=True,
        )
        db.add(owner_user)
        db.flush()

        org = Organization(id=uuid4(), name="Audit Org")
        db.add(org)
        db.flush()

        db.add(
            OrganizationMembership(
                user_id=owner_user.id, organization_id=org.id, role="owner"
            )
        )
        db.commit()

        org_id = org.id
        owner_id = owner_user.id

    current_principal_state = {
        "user_id": owner_id,
        "email": "owner@orbit.test",
        "uid": "uid-audit-owner",
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
            "engine": engine,
            "session_factory": session_factory,
            "org_id": org_id,
            "owner_id": owner_id,
        }

    app.dependency_overrides.clear()
    engine.dispose()


def test_connection_audit_events_created_and_deleted(audit_test_env):
    client = audit_test_env["client"]
    org_id = str(audit_test_env["org_id"])
    owner_id = audit_test_env["owner_id"]

    # 1. Create connection
    create_res = client.post(
        "/api/v1/connections",
        headers={"X-Organization-ID": org_id},
        json={
            "provider_key": "google_ads",
            "display_name": "Production Google Ads",
            "connection_type": "oauth",
        },
    )
    assert create_res.status_code == 201
    conn_id = create_res.json()["id"]

    # Verify audit event for connection.created
    with audit_test_env["session_factory"]() as db:
        events = (
            db.query(AuditEvent)
            .filter_by(
                organization_id=audit_test_env["org_id"],
                event_type="connection.created",
            )
            .all()
        )
        assert len(events) == 1
        ev = events[0]
        assert ev.actor_user_id == owner_id
        assert ev.target_type == "connection"
        assert ev.target_id == conn_id
        assert ev.created_at is not None
        assert ev.metadata_json == {
            "provider_key": "google_ads",
            "display_name": "Production Google Ads",
            "connection_type": "oauth",
        }

    # 2. Delete connection
    delete_res = client.delete(
        f"/api/v1/connections/{conn_id}",
        headers={"X-Organization-ID": org_id},
    )
    assert delete_res.status_code == 204

    # Verify audit event for connection.deleted
    with audit_test_env["session_factory"]() as db:
        del_events = (
            db.query(AuditEvent)
            .filter_by(
                organization_id=audit_test_env["org_id"],
                event_type="connection.deleted",
            )
            .all()
        )
        assert len(del_events) == 1
        del_ev = del_events[0]
        assert del_ev.actor_user_id == owner_id
        assert del_ev.target_type == "connection"
        assert del_ev.target_id == conn_id
        assert del_ev.created_at is not None
        assert del_ev.metadata_json == {
            "provider_key": "google_ads",
            "display_name": "Production Google Ads",
        }


def test_custom_connector_audit_events_and_secret_redaction(audit_test_env):
    client = audit_test_env["client"]
    org_id = str(audit_test_env["org_id"])
    owner_id = audit_test_env["owner_id"]

    # 1. Create custom connector with secret and headers
    create_res = client.post(
        "/api/v1/custom-connectors",
        headers={"X-Organization-ID": org_id},
        json={
            "name": "Secure Webhook Connector",
            "base_url": "https://api.securecorp.test",
            "endpoint_path": "/webhooks/v1",
            "auth_type": "bearer",
            "secret": "super_secret_bearer_token_xyz987",
            "secret_label": "Corporate API Key",
            "headers": {"X-Custom-Secret": "sensitive_header_value"},
        },
    )
    assert create_res.status_code == 201
    connector_id = create_res.json()["id"]

    # Verify audit event for custom_connector.created
    with audit_test_env["session_factory"]() as db:
        events = (
            db.query(AuditEvent)
            .filter_by(
                organization_id=audit_test_env["org_id"],
                event_type="custom_connector.created",
            )
            .all()
        )
        assert len(events) == 1
        ev = events[0]
        assert ev.actor_user_id == owner_id
        assert ev.target_type == "custom_connector"
        assert ev.target_id == connector_id
        assert ev.created_at is not None

        # Verify allowed metadata present
        assert ev.metadata_json["name"] == "Secure Webhook Connector"
        assert ev.metadata_json["base_url"] == "https://api.securecorp.test/"
        assert ev.metadata_json["endpoint_path"] == "/webhooks/v1"
        assert ev.metadata_json["auth_type"] == "bearer"
        assert ev.metadata_json["secret_label"] == "Corporate API Key"

        # Verify strict secret redaction from metadata_json
        raw_meta = str(ev.metadata_json)
        assert "super_secret_bearer_token_xyz987" not in raw_meta
        assert "sensitive_header_value" not in raw_meta
        assert "secret" not in ev.metadata_json
        assert "secret_encrypted" not in ev.metadata_json
        assert "headers" not in ev.metadata_json

    # 2. Delete custom connector
    delete_res = client.delete(
        f"/api/v1/custom-connectors/{connector_id}",
        headers={"X-Organization-ID": org_id},
    )
    assert delete_res.status_code == 204

    # Verify audit event for custom_connector.deleted
    with audit_test_env["session_factory"]() as db:
        del_events = (
            db.query(AuditEvent)
            .filter_by(
                organization_id=audit_test_env["org_id"],
                event_type="custom_connector.deleted",
            )
            .all()
        )
        assert len(del_events) == 1
        del_ev = del_events[0]
        assert del_ev.actor_user_id == owner_id
        assert del_ev.target_type == "custom_connector"
        assert del_ev.target_id == connector_id
        assert del_ev.metadata_json == {
            "name": "Secure Webhook Connector",
            "base_url": "https://api.securecorp.test/",
            "auth_type": "bearer",
        }


def test_connection_creation_audit_failure_rolls_back_mutation(audit_test_env):
    client = audit_test_env["client"]
    org_id = str(audit_test_env["org_id"])

    # Install SQLite trigger to abort audit insertion for connection.created
    with audit_test_env["engine"].begin() as conn:
        conn.execute(
            text(
                "CREATE TRIGGER abort_conn_audit "
                "BEFORE INSERT ON audit_events "
                "WHEN NEW.event_type = 'connection.created' "
                "BEGIN SELECT RAISE(ABORT, 'audit insertion aborted'); END;"
            )
        )

    res = client.post(
        "/api/v1/connections",
        headers={"X-Organization-ID": org_id},
        json={
            "provider_key": "google_ads",
            "display_name": "Failing Audit Conn",
            "connection_type": "oauth",
        },
    )
    assert res.status_code == 500

    # Ensure IntegrationConnection was rolled back and does not exist in DB
    with audit_test_env["session_factory"]() as db:
        row = (
            db.query(IntegrationConnection)
            .filter_by(display_name="Failing Audit Conn")
            .one_or_none()
        )
        assert row is None


def test_connection_deletion_audit_failure_rolls_back_mutation(audit_test_env):
    client = audit_test_env["client"]
    org_id = str(audit_test_env["org_id"])

    # Seed an existing connection directly
    with audit_test_env["session_factory"]() as db:
        conn_row = IntegrationConnection(
            id="conn-rollback-test",
            organization_id=audit_test_env["org_id"],
            provider_key="google_ads",
            display_name="Persistent Conn",
            status="active",
            connection_type="oauth",
            config_json={},
        )
        db.add(conn_row)
        db.commit()

    # Install trigger to abort audit insertion on connection.deleted
    with audit_test_env["engine"].begin() as conn:
        conn.execute(
            text(
                "CREATE TRIGGER abort_conn_del_audit "
                "BEFORE INSERT ON audit_events "
                "WHEN NEW.event_type = 'connection.deleted' "
                "BEGIN SELECT RAISE(ABORT, 'audit deletion aborted'); END;"
            )
        )

    res = client.delete(
        "/api/v1/connections/conn-rollback-test",
        headers={"X-Organization-ID": org_id},
    )
    assert res.status_code == 500

    # Ensure IntegrationConnection was NOT deleted (rollback preserved the row)
    with audit_test_env["session_factory"]() as db:
        row = (
            db.query(IntegrationConnection)
            .filter_by(id="conn-rollback-test")
            .one_or_none()
        )
        assert row is not None
        assert row.display_name == "Persistent Conn"


def test_custom_connector_audit_failure_rolls_back_mutations(audit_test_env):
    client = audit_test_env["client"]
    org_id = str(audit_test_env["org_id"])

    # 1. Rollback on creation
    with audit_test_env["engine"].begin() as conn:
        conn.execute(
            text(
                "CREATE TRIGGER abort_custom_conn_create_audit "
                "BEFORE INSERT ON audit_events "
                "WHEN NEW.event_type = 'custom_connector.created' "
                "BEGIN SELECT RAISE(ABORT, 'custom connector audit aborted'); END;"
            )
        )

    create_res = client.post(
        "/api/v1/custom-connectors",
        headers={"X-Organization-ID": org_id},
        json={
            "name": "Failing Custom Connector",
            "base_url": "https://api.failtest.test",
            "auth_type": "none",
        },
    )
    assert create_res.status_code == 500

    with audit_test_env["session_factory"]() as db:
        row = (
            db.query(CustomConnector)
            .filter_by(name="Failing Custom Connector")
            .one_or_none()
        )
        assert row is None

    # Drop creation trigger
    with audit_test_env["engine"].begin() as conn:
        conn.execute(text("DROP TRIGGER abort_custom_conn_create_audit;"))

    # Seed a custom connector
    with audit_test_env["session_factory"]() as db:
        c_row = CustomConnector(
            id="connector-del-rollback",
            organization_id=audit_test_env["org_id"],
            name="Persistent Custom Connector",
            base_url="https://api.persist.test",
            auth_type="none",
        )
        db.add(c_row)
        db.commit()

    # Install trigger to abort audit insertion on custom_connector.deleted
    with audit_test_env["engine"].begin() as conn:
        conn.execute(
            text(
                "CREATE TRIGGER abort_custom_del_audit "
                "BEFORE INSERT ON audit_events "
                "WHEN NEW.event_type = 'custom_connector.deleted' "
                "BEGIN SELECT RAISE(ABORT, 'custom del audit aborted'); END;"
            )
        )

    del_res = client.delete(
        "/api/v1/custom-connectors/connector-del-rollback",
        headers={"X-Organization-ID": org_id},
    )
    assert del_res.status_code == 500

    # Ensure CustomConnector was NOT deleted (rollback preserved the row)
    with audit_test_env["session_factory"]() as db:
        c_check = (
            db.query(CustomConnector)
            .filter_by(id="connector-del-rollback")
            .one_or_none()
        )
        assert c_check is not None
        assert c_check.name == "Persistent Custom Connector"

