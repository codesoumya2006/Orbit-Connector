from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.context import (
    OrganizationContext,
    get_organization_context,
    require_permission,
)
from app.core.database import Base, get_db
from app.models.entities import Organization, OrganizationMembership, User


@pytest.fixture
def context_test_env():
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
        db.add_all([owner_user, viewer_user, outsider_user])
        db.flush()

        org = Organization(id=uuid4(), name="Acme Logistics")
        db.add(org)
        db.flush()

        db.add(OrganizationMembership(user_id=owner_user.id, organization_id=org.id, role="owner"))
        db.add(OrganizationMembership(user_id=viewer_user.id, organization_id=org.id, role="viewer"))
        db.commit()

        org_id = org.id
        owner_id = owner_user.id
        viewer_id = viewer_user.id
        outsider_id = outsider_user.id

    from app.core.firebase_auth import AuthenticatedPrincipal, get_current_principal

    app = FastAPI()

    # Approved legacy query selector routes (both unprefixed and /api/v1 prefixed)
    @app.get("/connections")
    @app.get("/api/v1/connections")
    def get_connections(
        context: OrganizationContext = Depends(get_organization_context),
    ):
        return {"organization_id": str(context.organization_id), "role": context.role}

    @app.get("/custom-connectors")
    @app.get("/api/v1/custom-connectors")
    def get_custom_connectors(
        context: OrganizationContext = Depends(get_organization_context),
    ):
        return {"organization_id": str(context.organization_id), "role": context.role}

    # General route (header-only selector)
    @app.get("/general-resource")
    def get_general_resource(
        context: OrganizationContext = Depends(get_organization_context),
    ):
        return {
            "organization_id": str(context.organization_id),
            "role": context.role,
            "user_id": str(context.user_id),
        }

    # Path parameter selector route
    @app.get("/organizations/{organization_id}/scoped-path")
    def get_scoped_path_resource(
        context: OrganizationContext = Depends(get_organization_context),
    ):
        return {
            "organization_id": str(context.organization_id),
            "role": context.role,
        }

    # Permission-protected write route
    @app.post("/organizations/{organization_id}/protected-write")
    def post_protected_write(
        context: OrganizationContext = Depends(require_permission("connector:write")),
    ):
        return {"status": "ok", "role": context.role}

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
            "org_id": org_id,
            "owner_id": owner_id,
            "viewer_id": viewer_id,
            "outsider_id": outsider_id,
            "current_principal_state": current_principal_state,
        }

    app.dependency_overrides.clear()
    engine.dispose()


def test_missing_organization_selector_returns_400(context_test_env):
    client = context_test_env["client"]
    response = client.get("/general-resource")
    assert response.status_code == 400
    assert response.json()["detail"] == "Organization selector is required."


def test_query_selector_works_on_approved_legacy_get_routes(context_test_env):
    client = context_test_env["client"]
    org_id = str(context_test_env["org_id"])

    # 1. GET /connections with query organization_id
    res_conn = client.get(f"/connections?organization_id={org_id}")
    assert res_conn.status_code == 200
    assert res_conn.json()["organization_id"] == org_id
    assert res_conn.json()["role"] == "owner"

    # 2. GET /custom-connectors with query organization_id
    res_custom = client.get(f"/custom-connectors?organization_id={org_id}")
    assert res_custom.status_code == 200
    assert res_custom.json()["organization_id"] == org_id
    assert res_custom.json()["role"] == "owner"


def test_query_selector_rejected_on_unrelated_routes(context_test_env):
    client = context_test_env["client"]
    org_id = str(context_test_env["org_id"])

    # Attempting to supply query selector on /general-resource returns HTTP 400
    response = client.get(f"/general-resource?organization_id={org_id}")
    assert response.status_code == 400
    assert "Query parameter 'organization_id' is only supported" in response.json()["detail"]


def test_header_selector_works_on_general_routes(context_test_env):
    client = context_test_env["client"]
    org_id = str(context_test_env["org_id"])

    response = client.get("/general-resource", headers={"X-Organization-ID": org_id})
    assert response.status_code == 200
    assert response.json()["organization_id"] == org_id
    assert response.json()["role"] == "owner"


def test_path_plus_matching_header_works(context_test_env):
    client = context_test_env["client"]
    org_id = str(context_test_env["org_id"])

    response = client.get(
        f"/organizations/{org_id}/scoped-path",
        headers={"X-Organization-ID": org_id},
    )
    assert response.status_code == 200
    assert response.json()["organization_id"] == org_id


def test_conflicting_permitted_selectors_returns_400(context_test_env):
    client = context_test_env["client"]
    org_id1 = str(context_test_env["org_id"])
    org_id2 = str(uuid4())

    # Conflicting query and header on approved legacy route
    res_legacy = client.get(
        f"/connections?organization_id={org_id1}",
        headers={"X-Organization-ID": org_id2},
    )
    assert res_legacy.status_code == 400
    assert "Conflicting organization selectors" in res_legacy.json()["detail"]

    # Conflicting path and header
    res_path = client.get(
        f"/organizations/{org_id1}/scoped-path",
        headers={"X-Organization-ID": org_id2},
    )
    assert res_path.status_code == 400
    assert "Conflicting organization selectors" in res_path.json()["detail"]


def test_malformed_uuid_returns_422(context_test_env):
    client = context_test_env["client"]

    # In header
    res_header = client.get("/general-resource", headers={"X-Organization-ID": "not-a-valid-uuid"})
    assert res_header.status_code == 422
    assert "Invalid organization ID format" in res_header.json()["detail"]

    # In query on allowed route
    res_query = client.get("/connections?organization_id=123-bad-id")
    assert res_query.status_code == 422
    assert "Invalid organization ID format" in res_query.json()["detail"]

    # In path
    res_path = client.get("/organizations/invalid-uuid/scoped-path")
    assert res_path.status_code == 422
    assert "Invalid organization ID format" in res_path.json()["detail"]


def test_nonmember_access_returns_anti_enumeration_404(context_test_env):
    client = context_test_env["client"]
    org_id = str(context_test_env["org_id"])

    # Switch principal to outsider
    context_test_env["current_principal_state"].update(
        {
            "user_id": context_test_env["outsider_id"],
            "email": "outsider@orbit.test",
            "uid": "uid-outsider",
        }
    )

    response = client.get(f"/organizations/{org_id}/scoped-path")
    assert response.status_code == 404
    assert response.json()["detail"] == "Resource not found."


def test_unregistered_organization_returns_404(context_test_env):
    client = context_test_env["client"]
    random_org_id = str(uuid4())

    response = client.get(f"/organizations/{random_org_id}/scoped-path")
    assert response.status_code == 404
    assert response.json()["detail"] == "Resource not found."


def test_require_permission_enforcement(context_test_env):
    client = context_test_env["client"]
    org_id = str(context_test_env["org_id"])

    # Owner has connector:write -> succeeds (200)
    res_owner = client.post(f"/organizations/{org_id}/protected-write")
    assert res_owner.status_code == 200
    assert res_owner.json()["role"] == "owner"

    # Switch to viewer
    context_test_env["current_principal_state"].update(
        {
            "user_id": context_test_env["viewer_id"],
            "email": "viewer@orbit.test",
            "uid": "uid-viewer",
        }
    )

    # Viewer lacks connector:write -> 403 Forbidden
    res_viewer = client.post(f"/organizations/{org_id}/protected-write")
    assert res_viewer.status_code == 403
    assert res_viewer.json()["detail"] == "Permission 'connector:write' denied for role 'viewer'."
