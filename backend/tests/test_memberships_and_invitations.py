from datetime import datetime, timedelta, timezone
import hashlib
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.organizations import router as organizations_router
from app.core.database import Base, get_db
from app.models.entities import (
    AuditEvent,
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    User,
)


@pytest.fixture
def membership_api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        owner_1 = User(
            id=uuid4(),
            firebase_uid="uid-owner-1",
            email="owner1@orbit.test",
            email_verified=True,
        )
        owner_2 = User(
            id=uuid4(),
            firebase_uid="uid-owner-2",
            email="owner2@orbit.test",
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
        unverified_user = User(
            id=uuid4(),
            firebase_uid="uid-unverified",
            email="unverified@orbit.test",
            email_verified=False,
        )
        db.add_all([owner_1, owner_2, admin_user, viewer_user, outsider_user, unverified_user])
        db.flush()

        org_a = Organization(id=uuid4(), name="Test Org Alpha")
        org_b = Organization(id=uuid4(), name="Test Org Beta")
        db.add_all([org_a, org_b])
        db.flush()

        db.add(OrganizationMembership(user_id=owner_1.id, organization_id=org_a.id, role="owner"))
        db.add(OrganizationMembership(user_id=owner_2.id, organization_id=org_a.id, role="owner"))
        db.add(OrganizationMembership(user_id=admin_user.id, organization_id=org_a.id, role="admin"))
        db.add(OrganizationMembership(user_id=viewer_user.id, organization_id=org_a.id, role="viewer"))

        db.add(OrganizationMembership(user_id=outsider_user.id, organization_id=org_b.id, role="owner"))
        db.commit()

        org_a_id = org_a.id
        org_b_id = org_b.id
        owner_1_id = owner_1.id
        owner_2_id = owner_2.id
        admin_id = admin_user.id
        viewer_id = viewer_user.id
        outsider_id = outsider_user.id
        unverified_id = unverified_user.id

    from app.core.firebase_auth import AuthenticatedPrincipal, get_current_principal

    app = FastAPI()
    app.include_router(organizations_router)

    current_state = {
        "user_id": owner_1_id,
        "email": "owner1@orbit.test",
        "email_verified": True,
        "uid": "uid-owner-1",
    }

    def override_get_db():
        with session_factory() as db:
            yield db

    def override_get_principal():
        return AuthenticatedPrincipal(
            uid=current_state["uid"],
            auth_time=datetime.now(timezone.utc),
            user=User(
                id=current_state["user_id"],
                firebase_uid=current_state["uid"],
                email=current_state["email"],
                email_verified=current_state["email_verified"],
            ),
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_principal] = override_get_principal

    with TestClient(app, raise_server_exceptions=False) as client:
        yield {
            "client": client,
            "engine": engine,
            "session_factory": session_factory,
            "org_id": org_a_id,
            "org_b_id": org_b_id,
            "owner_1_id": owner_1_id,
            "owner_2_id": owner_2_id,
            "admin_id": admin_id,
            "viewer_id": viewer_id,
            "outsider_id": outsider_id,
            "unverified_id": unverified_id,
            "current_state": current_state,
        }

    app.dependency_overrides.clear()
    engine.dispose()


def test_list_members_allowed_for_all_org_roles(membership_api):
    client = membership_api["client"]
    org_id = str(membership_api["org_id"])

    # Owner can read
    res_owner = client.get(f"/organizations/{org_id}/members")
    assert res_owner.status_code == 200
    assert len(res_owner.json()) == 4

    # Admin can read
    membership_api["current_state"].update(
        {"user_id": membership_api["admin_id"], "email": "admin@orbit.test", "uid": "uid-admin"}
    )
    res_admin = client.get(f"/organizations/{org_id}/members")
    assert res_admin.status_code == 200

    # Viewer can read
    membership_api["current_state"].update(
        {"user_id": membership_api["viewer_id"], "email": "viewer@orbit.test", "uid": "uid-viewer"}
    )
    res_viewer = client.get(f"/organizations/{org_id}/members")
    assert res_viewer.status_code == 200

    # Outsider receives 404 (anti-enumeration)
    membership_api["current_state"].update(
        {"user_id": membership_api["outsider_id"], "email": "outsider@orbit.test", "uid": "uid-outsider"}
    )
    res_out = client.get(f"/organizations/{org_id}/members")
    assert res_out.status_code == 404


def test_member_role_update_permissions_and_last_owner_protection(membership_api):
    client = membership_api["client"]
    org_id = str(membership_api["org_id"])
    viewer_id = str(membership_api["viewer_id"])
    owner_2_id = str(membership_api["owner_2_id"])
    owner_1_id = str(membership_api["owner_1_id"])

    # 1. Admin cannot promote viewer to admin (elevated action -> 403)
    membership_api["current_state"].update(
        {"user_id": membership_api["admin_id"], "email": "admin@orbit.test", "uid": "uid-admin"}
    )
    res_fail = client.patch(
        f"/organizations/{org_id}/members/{viewer_id}",
        json={"role": "admin"},
    )
    assert res_fail.status_code == 403

    # 2. Owner promotes viewer to admin (succeeds -> 200)
    membership_api["current_state"].update(
        {"user_id": membership_api["owner_1_id"], "email": "owner1@orbit.test", "uid": "uid-owner-1"}
    )
    res_ok = client.patch(
        f"/organizations/{org_id}/members/{viewer_id}",
        json={"role": "admin"},
    )
    assert res_ok.status_code == 200
    assert res_ok.json()["role"] == "admin"

    # 3. Demote owner_2 to admin when 2 owners exist (succeeds -> 200)
    res_demote = client.patch(
        f"/organizations/{org_id}/members/{owner_2_id}",
        json={"role": "admin"},
    )
    assert res_demote.status_code == 200

    # 4. Now only owner_1 is owner. Demoting owner_1 is rejected by Last-Owner protection (-> 400)
    res_last = client.patch(
        f"/organizations/{org_id}/members/{owner_1_id}",
        json={"role": "admin"},
    )
    assert res_last.status_code == 400
    assert "Cannot demote the last owner" in res_last.json()["detail"]


def test_member_removal_permissions_and_last_owner_protection(membership_api):
    client = membership_api["client"]
    org_id = str(membership_api["org_id"])
    owner_1_id = str(membership_api["owner_1_id"])
    owner_2_id = str(membership_api["owner_2_id"])
    admin_id = str(membership_api["admin_id"])
    viewer_id = str(membership_api["viewer_id"])

    # Admin removes viewer (allowed -> 204)
    membership_api["current_state"].update(
        {"user_id": membership_api["admin_id"], "email": "admin@orbit.test", "uid": "uid-admin"}
    )
    res_del_viewer = client.delete(f"/organizations/{org_id}/members/{viewer_id}")
    assert res_del_viewer.status_code == 204

    # Admin cannot remove owner_2 (403)
    res_del_owner = client.delete(f"/organizations/{org_id}/members/{owner_2_id}")
    assert res_del_owner.status_code == 403

    # Switch to Owner 1
    membership_api["current_state"].update(
        {"user_id": membership_api["owner_1_id"], "email": "owner1@orbit.test", "uid": "uid-owner-1"}
    )
    # Owner removes owner_2 (succeeds -> 204 because owner_1 remains)
    res_owner_del_owner = client.delete(f"/organizations/{org_id}/members/{owner_2_id}")
    assert res_owner_del_owner.status_code == 204

    # Attempting to remove last remaining owner (owner_1) fails (-> 400)
    res_last_owner = client.delete(f"/organizations/{org_id}/members/{owner_1_id}")
    assert res_last_owner.status_code == 400
    assert "Cannot remove the last owner" in res_last_owner.json()["detail"]


def test_invitation_lifecycle_rbac_expiration_and_acceptance(membership_api):
    client = membership_api["client"]
    org_id = str(membership_api["org_id"])

    # 1. Admin creates viewer invitation -> 201
    membership_api["current_state"].update(
        {"user_id": membership_api["admin_id"], "email": "admin@orbit.test", "uid": "uid-admin"}
    )
    res_invite = client.post(
        f"/organizations/{org_id}/invitations",
        json={"email": "Invited@Orbit.Test", "role": "viewer"},
    )
    assert res_invite.status_code == 201
    inv_data = res_invite.json()
    token = inv_data["invitation_token"]
    inv_id = inv_data["id"]
    assert inv_data["email"] == "invited@orbit.test"
    assert "token_hash" not in res_invite.text

    # Admin cannot invite admin (elevated -> 403)
    res_fail_invite = client.post(
        f"/organizations/{org_id}/invitations",
        json={"email": "newadmin@orbit.test", "role": "admin"},
    )
    assert res_fail_invite.status_code == 403

    # Owner can invite admin (elevated -> 201)
    membership_api["current_state"].update(
        {"user_id": membership_api["owner_1_id"], "email": "owner1@orbit.test", "uid": "uid-owner-1"}
    )
    res_owner_invite = client.post(
        f"/organizations/{org_id}/invitations",
        json={"email": "owner_invitee@orbit.test", "role": "admin"},
    )
    assert res_owner_invite.status_code == 201

    # 2. List invitations
    res_list = client.get(f"/organizations/{org_id}/invitations")
    assert res_list.status_code == 200
    assert any(inv["id"] == inv_id for inv in res_list.json())

    # 3. Acceptance with mismatched email fails (400)
    membership_api["current_state"].update(
        {"user_id": membership_api["outsider_id"], "email": "wrong@orbit.test", "uid": "uid-outsider", "email_verified": True}
    )
    res_mismatch = client.post("/invitations/accept", json={"token": token})
    assert res_mismatch.status_code == 400
    assert "email does not match" in res_mismatch.json()["detail"]

    # 4. Acceptance with unverified email fails (400)
    membership_api["current_state"].update(
        {"user_id": membership_api["unverified_id"], "email": "invited@orbit.test", "uid": "uid-unverified", "email_verified": False}
    )
    res_unverified = client.post("/invitations/accept", json={"token": token})
    assert res_unverified.status_code == 400
    assert "verified" in res_unverified.json()["detail"]

    # 5. Acceptance with normalized matching verified email succeeds (201)
    membership_api["current_state"].update(
        {"user_id": membership_api["outsider_id"], "email": "   InViTeD@orbit.test  ", "uid": "uid-outsider", "email_verified": True}
    )
    res_accept = client.post("/invitations/accept", json={"token": token})
    assert res_accept.status_code == 201
    assert res_accept.json()["role"] == "viewer"

    # 6. Re-accepting already accepted invitation fails (400)
    res_repeat = client.post("/invitations/accept", json={"token": token})
    assert res_repeat.status_code == 400
    assert "already been accepted" in res_repeat.json()["detail"]

    # Verify membership row was created and AuditEvent recorded in database
    with membership_api["session_factory"]() as db:
        mem = (
            db.query(OrganizationMembership)
            .filter_by(user_id=membership_api["outsider_id"], organization_id=membership_api["org_id"])
            .one_or_none()
        )
        assert mem is not None
        assert mem.role == "viewer"

        audit = (
            db.query(AuditEvent)
            .filter_by(event_type="invitation.accepted", target_id=inv_id)
            .one_or_none()
        )
        assert audit is not None


def test_invitation_revocation_and_expiration(membership_api):
    client = membership_api["client"]
    org_id = str(membership_api["org_id"])

    # Owner creates invitation
    membership_api["current_state"].update(
        {"user_id": membership_api["owner_1_id"], "email": "owner1@orbit.test", "uid": "uid-owner-1", "email_verified": True}
    )
    res_inv = client.post(
        f"/organizations/{org_id}/invitations",
        json={"email": "to_revoke@orbit.test", "role": "viewer"},
    )
    assert res_inv.status_code == 201
    token = res_inv.json()["invitation_token"]
    inv_id = res_inv.json()["id"]

    # Revoke invitation
    res_rev = client.delete(f"/organizations/{org_id}/invitations/{inv_id}")
    assert res_rev.status_code == 204

    # Accepting revoked invitation fails (400)
    membership_api["current_state"].update(
        {"user_id": membership_api["outsider_id"], "email": "to_revoke@orbit.test", "uid": "uid-outsider", "email_verified": True}
    )
    res_acc = client.post("/invitations/accept", json={"token": token})
    assert res_acc.status_code == 400
    assert "revoked" in res_acc.json()["detail"]


def test_cross_tenant_invitation_access_fails_closed(membership_api):
    client = membership_api["client"]
    org_a = str(membership_api["org_id"])
    org_b = str(membership_api["org_b_id"])

    # Org A creates an invitation
    membership_api["current_state"].update(
        {"user_id": membership_api["owner_1_id"], "email": "owner1@orbit.test", "uid": "uid-owner-1", "email_verified": True}
    )
    res_inv = client.post(
        f"/organizations/{org_a}/invitations",
        json={"email": "cross_tenant@orbit.test", "role": "viewer"},
    )
    assert res_inv.status_code == 201
    inv_id = res_inv.json()["id"]

    # Switch caller to Org B owner
    membership_api["current_state"].update(
        {"user_id": membership_api["outsider_id"], "email": "outsider@orbit.test", "uid": "uid-outsider", "email_verified": True}
    )
    # Attempting to delete Org A's invitation via Org B context returns 404
    res_del_cross = client.delete(f"/organizations/{org_b}/invitations/{inv_id}")
    assert res_del_cross.status_code == 404
    assert res_del_cross.json()["detail"] == "Invitation not found."


def test_viewer_forbidden_from_invitation_management(membership_api):
    client = membership_api["client"]
    org_id = str(membership_api["org_id"])

    # Switch to viewer
    membership_api["current_state"].update(
        {"user_id": membership_api["viewer_id"], "email": "viewer@orbit.test", "uid": "uid-viewer", "email_verified": True}
    )

    # Viewer cannot list invitations -> 403
    res_list = client.get(f"/organizations/{org_id}/invitations")
    assert res_list.status_code == 403

    # Viewer cannot create invitation -> 403
    res_create = client.post(
        f"/organizations/{org_id}/invitations",
        json={"email": "viewer_attempt@orbit.test", "role": "viewer"},
    )
    assert res_create.status_code == 403

    # Viewer cannot revoke invitation -> 403
    res_revoke = client.delete(f"/organizations/{org_id}/invitations/{uuid4()}")
    assert res_revoke.status_code == 403


def test_expired_invitation_rejected_on_accept(membership_api):
    client = membership_api["client"]
    now = datetime.now(timezone.utc)
    expired_token = "expired-token-12345"
    token_hash = hashlib.sha256(expired_token.encode()).hexdigest()

    with membership_api["session_factory"]() as db:
        expired_inv = OrganizationInvitation(
            id=uuid4(),
            organization_id=membership_api["org_id"],
            created_by_user_id=membership_api["owner_1_id"],
            normalized_email="expired_target@orbit.test",
            role="viewer",
            token_hash=token_hash,
            expires_at=now - timedelta(days=2),
            created_at=now - timedelta(days=9),
        )
        db.add(expired_inv)
        db.commit()

    # Attempt to accept expired invitation
    membership_api["current_state"].update(
        {"user_id": membership_api["outsider_id"], "email": "expired_target@orbit.test", "uid": "uid-outsider", "email_verified": True}
    )
    res_accept = client.post("/invitations/accept", json={"token": expired_token})
    assert res_accept.status_code == 400
    assert "expired" in res_accept.json()["detail"]


def test_audit_failure_rolls_back_invitation_creation(membership_api):
    client = membership_api["client"]
    org_id = str(membership_api["org_id"])

    # Create trigger on SQLite to abort audit event insertion for invitations
    with membership_api["engine"].begin() as conn:
        conn.execute(
            text(
                "CREATE TRIGGER abort_invitation_audit "
                "BEFORE INSERT ON audit_events "
                "WHEN NEW.event_type = 'invitation.created' "
                "BEGIN SELECT RAISE(ABORT, 'audit insertion aborted'); END;"
            )
        )

    # Attempt to create invitation as Owner
    membership_api["current_state"].update(
        {"user_id": membership_api["owner_1_id"], "email": "owner1@orbit.test", "uid": "uid-owner-1", "email_verified": True}
    )
    response = client.post(
        f"/organizations/{org_id}/invitations",
        json={"email": "rollback_test@orbit.test", "role": "viewer"},
    )
    assert response.status_code == 500

    # Verify invitation was rolled back and does not exist in DB
    with membership_api["session_factory"]() as db:
        inv = (
            db.query(OrganizationInvitation)
            .filter_by(normalized_email="rollback_test@orbit.test")
            .one_or_none()
        )
        assert inv is None
