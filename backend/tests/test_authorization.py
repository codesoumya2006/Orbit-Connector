from importlib import import_module, util


def _role_allows():
    assert util.find_spec("app.core.authorization"), (
        "Missing server-side organization authorization module"
    )
    authorization = import_module("app.core.authorization")
    assert hasattr(authorization, "role_allows"), (
        "Missing server-side organization role policy: role_allows"
    )
    return authorization.role_allows


def test_owner_and_admin_can_manage_connectors_but_viewer_cannot():
    role_allows = _role_allows()

    assert role_allows("owner", "connector:write")
    assert role_allows("admin", "connector:write")
    assert not role_allows("viewer", "connector:write")


def test_all_roles_can_read_connector_metadata():
    role_allows = _role_allows()

    for role in ("owner", "admin", "viewer"):
        assert role_allows(role, "connector:read")


def test_only_owner_can_manage_elevated_membership():
    role_allows = _role_allows()

    assert role_allows("owner", "membership:manage_elevated")
    assert not role_allows("admin", "membership:manage_elevated")
    assert not role_allows("viewer", "membership:manage_elevated")


def test_viewer_cannot_read_credentials_and_unknown_roles_fail_closed():
    role_allows = _role_allows()

    assert not role_allows("viewer", "credential:read")
    assert not role_allows("unknown", "connector:read")
    assert not role_allows("owner", "unlisted:permission")
