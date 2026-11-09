from collections.abc import Mapping


_ROLE_PERMISSIONS: Mapping[str, frozenset[str]] = {
    "owner": frozenset(
        {
            "connector:read",
            "connector:write",
            "membership:read",
            "membership:manage_viewer",
            "membership:manage_elevated",
            "invitation:read",
            "invitation:create_viewer",
            "invitation:create_elevated",
        }
    ),
    "admin": frozenset(
        {
            "connector:read",
            "connector:write",
            "membership:read",
            "membership:manage_viewer",
            "invitation:read",
            "invitation:create_viewer",
        }
    ),
    "viewer": frozenset({"connector:read", "membership:read"}),
}


def role_allows(role: str, permission: str) -> bool:
    return permission in _ROLE_PERMISSIONS.get(role, frozenset())
