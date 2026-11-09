import re

from sqlalchemy import CheckConstraint, DateTime, Uuid

import app.models.entities as entities


def _model(name):
    assert hasattr(entities, name), (
        f"Missing auth schema model: app.models.entities.{name}"
    )
    return getattr(entities, name)


def test_user_and_organization_use_uuid_primary_keys():
    for model_name in ("User", "Organization"):
        model = _model(model_name)
        assert isinstance(model.__table__.c.id.type, Uuid), (
            f"{model_name}.id must use a UUID type"
        )
        assert model.__table__.c.id.primary_key


def test_user_firebase_uid_is_unique_and_indexed():
    user = _model("User")
    firebase_uid = user.__table__.c.firebase_uid

    assert firebase_uid.unique, "User.firebase_uid must be unique"
    assert any(
        firebase_uid.name in index.columns
        for index in user.__table__.indexes
    ), "User.firebase_uid must be indexed"


def test_membership_has_composite_primary_key_and_role_constraint():
    membership = _model("OrganizationMembership")
    table = membership.__table__

    assert [column.name for column in table.primary_key.columns] == [
        "user_id",
        "organization_id",
    ]
    role_checks = [
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    ]
    assert any(
        re.search(
            r"\brole\b\s+in\s*\(\s*['\"]owner['\"]\s*,\s*['\"]admin['\"]\s*,"
            r"\s*['\"]viewer['\"]\s*\)",
            check,
            re.IGNORECASE,
        )
        for check in role_checks
    ), "Membership role must be constrained to owner, admin, or viewer"


def test_invitation_uses_uuid_primary_key_and_unique_token_hash():
    invitation = _model("OrganizationInvitation")
    table = invitation.__table__

    assert isinstance(table.c.id.type, Uuid), (
        "OrganizationInvitation.id must use a UUID type"
    )
    assert table.c.id.primary_key
    assert table.c.token_hash.unique, (
        "OrganizationInvitation.token_hash must be unique"
    )
    assert table.c.token_hash.nullable is False


def test_audit_event_has_timestamp_and_auth_time_fields():
    audit_event = _model("AuditEvent")
    table = audit_event.__table__

    assert isinstance(table.c.created_at.type, DateTime)
    assert isinstance(table.c.auth_time.type, DateTime)
