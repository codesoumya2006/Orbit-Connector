from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.authorization import role_allows
from app.core.database import get_db
from app.core.firebase_auth import AuthenticatedPrincipal, get_current_principal
from app.models.entities import Organization, OrganizationMembership


@dataclass(frozen=True)
class OrganizationContext:
    organization: Organization
    membership: OrganizationMembership
    principal: AuthenticatedPrincipal

    @property
    def role(self) -> str:
        return self.membership.role

    @property
    def organization_id(self) -> UUID:
        return self.organization.id

    @property
    def user_id(self) -> UUID:
        return self.principal.user.id

    def allows(self, permission: str) -> bool:
        return role_allows(self.role, permission)


_QUERY_SELECTOR_ALLOWED_ROUTES = frozenset(
    {"/connections", "/custom-connectors", "/api/v1/connections", "/api/v1/custom-connectors"}
)


def get_organization_context(
    request: Request,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> OrganizationContext:
    # 1. Collect non-empty selectors across Path, Header, and Query
    raw_path_id = request.path_params.get("organization_id")
    raw_header_id = request.headers.get("x-organization-id") or request.headers.get("X-Organization-ID")
    raw_query_id = request.query_params.get("organization_id")

    selectors: list[tuple[str, str]] = []
    if raw_path_id and str(raw_path_id).strip():
        selectors.append(("path", str(raw_path_id).strip()))
    if raw_header_id and str(raw_header_id).strip():
        selectors.append(("header", str(raw_header_id).strip()))

    if raw_query_id and str(raw_query_id).strip():
        normalized_path = request.url.path.rstrip("/")
        is_allowed_query_route = (
            request.method.upper() == "GET"
            and normalized_path in _QUERY_SELECTOR_ALLOWED_ROUTES
        )
        if not is_allowed_query_route:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Query parameter 'organization_id' is only supported on GET /connections and GET /custom-connectors.",
            )
        selectors.append(("query", str(raw_query_id).strip()))

    # Condition 3: Missing selector -> 400
    if not selectors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Organization selector is required.",
        )

    # Condition 1: Conflicting selectors -> 400 (never silently prioritize)
    distinct_values = {val.casefold() for _, val in selectors}
    if len(distinct_values) > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Conflicting organization selectors provided across path, header, or query.",
        )

    selected_raw = selectors[0][1]

    # Condition 2: Malformed UUID -> 422 Unprocessable Entity
    try:
        org_uuid = UUID(selected_raw)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid organization ID format; expected a valid UUID.",
        )

    # Condition 4: Non-member accessing an organization -> 404 (Fail-closed anti-enumeration)
    membership = (
        db.query(OrganizationMembership)
        .filter_by(user_id=principal.user.id, organization_id=org_uuid)
        .one_or_none()
    )
    if not membership:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource not found.",
        )

    # Resolve organization entity
    org = db.get(Organization, org_uuid)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource not found.",
        )

    return OrganizationContext(
        organization=org,
        membership=membership,
        principal=principal,
    )


def require_permission(permission: str):
    def dependency(
        context: OrganizationContext = Depends(get_organization_context),
    ) -> OrganizationContext:
        # Condition 5: Insufficient permissions -> 403
        if not context.allows(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission '{permission}' denied for role '{context.role}'.",
            )
        return context

    return dependency


def require_fresh_authentication(max_age_seconds: int = 300):
    def dependency(
        context: OrganizationContext = Depends(get_organization_context),
    ) -> OrganizationContext:
        auth_time = context.principal.auth_time
        if auth_time.tzinfo is None:
            auth_time = auth_time.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - auth_time).total_seconds()
        if elapsed > max_age_seconds:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Recent authentication required for this sensitive action. Please re-authenticate.",
                headers={"WWW-Authenticate": 'Bearer error="fresh_token_required"'},
            )
        return context

    return dependency
