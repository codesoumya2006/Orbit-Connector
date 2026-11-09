from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.context import OrganizationContext, require_permission
from app.core.database import get_db
from app.core.firebase_auth import AuthenticatedPrincipal, get_current_principal
from app.models.entities import (
    AuditEvent,
    Organization,
    OrganizationMembership,
    OrganizationInvitation,
    User,
)

router = APIRouter()


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)


class InvitationCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: Literal["owner", "admin", "viewer"] = "viewer"


class MemberRoleUpdate(BaseModel):
    role: Literal["owner", "admin", "viewer"]


class InvitationAccept(BaseModel):
    token: str = Field(min_length=1)


@router.get("/organizations")
def list_organizations(
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    db: Session = Depends(get_db),
):
    memberships = (
        db.query(OrganizationMembership, Organization)
        .join(Organization, OrganizationMembership.organization_id == Organization.id)
        .filter(OrganizationMembership.user_id == principal.user.id)
        .all()
    )
    return [
        {
            "id": str(org.id),
            "name": org.name,
            "role": membership.role,
        }
        for membership, org in memberships
    ]


@router.post("/organizations", status_code=status.HTTP_201_CREATED)
def create_organization(
    payload: OrganizationCreate,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    db: Session = Depends(get_db),
):
    org = Organization(id=uuid4(), name=payload.name)
    try:
        db.add(org)
        db.flush()

        membership = OrganizationMembership(
            user_id=principal.user.id,
            organization_id=org.id,
            role="owner",
        )
        db.add(membership)
        db.flush()

        audit = AuditEvent(
            actor_user_id=principal.user.id,
            organization_id=org.id,
            event_type="organization.created",
            target_type="organization",
            target_id=str(org.id),
            created_at=datetime.now(timezone.utc),
            metadata_json={"name": payload.name},
        )
        db.add(audit)
        db.commit()
        db.refresh(org)
        return {
            "id": str(org.id),
            "name": org.name,
            "role": "owner",
        }
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create organization.",
        ) from exc


@router.get("/organizations/{organization_id}/members")
def get_organization_members(
    context: OrganizationContext = Depends(require_permission("membership:read")),
    db: Session = Depends(get_db),
):
    members = (
        db.query(OrganizationMembership, User)
        .join(User, OrganizationMembership.user_id == User.id)
        .filter(OrganizationMembership.organization_id == context.organization_id)
        .all()
    )
    return [
        {
            "user_id": str(membership.user_id),
            "role": membership.role,
            "email": user.email,
            "display_name": user.display_name,
        }
        for membership, user in members
    ]


@router.patch("/organizations/{organization_id}/members/{target_user_id}")
def update_member_role(
    target_user_id: UUID,
    payload: MemberRoleUpdate,
    context: OrganizationContext = Depends(require_permission("membership:read")),
    db: Session = Depends(get_db),
):
    # Lock organization row to prevent concurrent race conditions
    db.query(Organization).filter_by(id=context.organization_id).with_for_update().first()

    target_membership = (
        db.query(OrganizationMembership)
        .filter_by(user_id=target_user_id, organization_id=context.organization_id)
        .one_or_none()
    )
    if not target_membership:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found in organization.",
        )

    # Permission boundaries:
    # Managing elevated roles (admin/owner) requires membership:manage_elevated (Owner only)
    is_elevated_action = (
        target_membership.role in ("owner", "admin") or payload.role in ("owner", "admin")
    )
    if is_elevated_action:
        if not context.allows("membership:manage_elevated"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission 'membership:manage_elevated' denied for role '{context.role}'.",
            )
    else:
        if not context.allows("membership:manage_viewer"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission 'membership:manage_viewer' denied for role '{context.role}'.",
            )

    # Last-owner protection: cannot demote the last remaining owner
    if target_membership.role == "owner" and payload.role != "owner":
        owner_count = (
            db.query(OrganizationMembership)
            .filter_by(organization_id=context.organization_id, role="owner")
            .count()
        )
        if owner_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot demote the last owner of an organization.",
            )

    try:
        old_role = target_membership.role
        target_membership.role = payload.role
        db.add(
            AuditEvent(
                actor_user_id=context.user_id,
                organization_id=context.organization_id,
                event_type="membership.role_updated",
                target_type="membership",
                target_id=str(target_user_id),
                created_at=datetime.now(timezone.utc),
                metadata_json={"old_role": old_role, "new_role": payload.role},
            )
        )
        db.commit()
        return {
            "user_id": str(target_user_id),
            "organization_id": str(context.organization_id),
            "role": payload.role,
        }
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update member role.",
        ) from exc


@router.delete("/organizations/{organization_id}/members/{target_user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    target_user_id: UUID,
    context: OrganizationContext = Depends(require_permission("membership:read")),
    db: Session = Depends(get_db),
):
    # Lock organization row to prevent concurrent race conditions
    db.query(Organization).filter_by(id=context.organization_id).with_for_update().first()

    target_membership = (
        db.query(OrganizationMembership)
        .filter_by(user_id=target_user_id, organization_id=context.organization_id)
        .one_or_none()
    )
    if not target_membership:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found in organization.",
        )

    # Permission check:
    if target_membership.role in ("owner", "admin"):
        if not context.allows("membership:manage_elevated"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission 'membership:manage_elevated' denied for role '{context.role}'.",
            )
    else:
        if not context.allows("membership:manage_viewer"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission 'membership:manage_viewer' denied for role '{context.role}'.",
            )

    # Last-owner protection: cannot remove the last owner
    if target_membership.role == "owner":
        owner_count = (
            db.query(OrganizationMembership)
            .filter_by(organization_id=context.organization_id, role="owner")
            .count()
        )
        if owner_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot remove the last owner of an organization.",
            )

    try:
        db.delete(target_membership)
        db.add(
            AuditEvent(
                actor_user_id=context.user_id,
                organization_id=context.organization_id,
                event_type="membership.removed",
                target_type="membership",
                target_id=str(target_user_id),
                created_at=datetime.now(timezone.utc),
                metadata_json={"removed_role": target_membership.role},
            )
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove member.",
        ) from exc


@router.get("/organizations/{organization_id}/invitations")
def list_invitations(
    context: OrganizationContext = Depends(require_permission("invitation:read")),
    db: Session = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    invitations = (
        db.query(OrganizationInvitation)
        .filter(
            OrganizationInvitation.organization_id == context.organization_id,
            OrganizationInvitation.accepted_at.is_(None),
            OrganizationInvitation.revoked_at.is_(None),
            OrganizationInvitation.expires_at > now,
        )
        .all()
    )
    return [
        {
            "id": str(inv.id),
            "email": inv.normalized_email,
            "role": inv.role,
            "expires_at": inv.expires_at.isoformat(),
            "created_at": inv.created_at.isoformat(),
        }
        for inv in invitations
    ]


@router.post("/organizations/{organization_id}/invitations", status_code=status.HTTP_201_CREATED)
def create_invitation(
    payload: InvitationCreate,
    context: OrganizationContext = Depends(require_permission("invitation:read")),
    db: Session = Depends(get_db),
):
    # Enforce permission based on target invitation role
    if payload.role in ("owner", "admin"):
        if not context.allows("invitation:create_elevated"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission 'invitation:create_elevated' denied for role '{context.role}'.",
            )
    else:
        if not context.allows("invitation:create_viewer"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission 'invitation:create_viewer' denied for role '{context.role}'.",
            )

    normalized_email = payload.email.strip().casefold()
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=7)

    invitation = OrganizationInvitation(
        id=uuid4(),
        organization_id=context.organization_id,
        created_by_user_id=context.user_id,
        normalized_email=normalized_email,
        role=payload.role,
        token_hash=token_hash,
        expires_at=expires_at,
        created_at=now,
    )
    try:
        db.add(invitation)
        db.flush()
        db.add(
            AuditEvent(
                actor_user_id=context.user_id,
                organization_id=context.organization_id,
                event_type="invitation.created",
                target_type="invitation",
                target_id=str(invitation.id),
                created_at=now,
                metadata_json={"email": normalized_email, "role": payload.role},
            )
        )
        db.commit()
        db.refresh(invitation)
        return {
            "id": str(invitation.id),
            "organization_id": str(context.organization_id),
            "email": normalized_email,
            "role": payload.role,
            "expires_at": expires_at.isoformat(),
            "invitation_token": raw_token,
        }
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create invitation.",
        ) from exc


@router.delete("/organizations/{organization_id}/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_invitation(
    invitation_id: UUID,
    context: OrganizationContext = Depends(require_permission("invitation:read")),
    db: Session = Depends(get_db),
):
    invitation = (
        db.query(OrganizationInvitation)
        .filter_by(id=invitation_id, organization_id=context.organization_id)
        .one_or_none()
    )
    if not invitation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitation not found.",
        )

    # Permission check to revoke:
    if invitation.role in ("owner", "admin"):
        if not context.allows("invitation:create_elevated"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission 'invitation:create_elevated' denied for role '{context.role}'.",
            )
    else:
        if not context.allows("invitation:create_viewer"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission 'invitation:create_viewer' denied for role '{context.role}'.",
            )

    now = datetime.now(timezone.utc)
    try:
        invitation.revoked_at = now
        db.add(
            AuditEvent(
                actor_user_id=context.user_id,
                organization_id=context.organization_id,
                event_type="invitation.revoked",
                target_type="invitation",
                target_id=str(invitation.id),
                created_at=now,
                metadata_json={"role": invitation.role},
            )
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to revoke invitation.",
        ) from exc


@router.post("/invitations/accept", status_code=status.HTTP_201_CREATED)
def accept_invitation(
    payload: InvitationAccept,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    db: Session = Depends(get_db),
):
    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    invitation = (
        db.query(OrganizationInvitation)
        .filter_by(token_hash=token_hash)
        .one_or_none()
    )
    if not invitation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitation not found or invalid.",
        )

    if invitation.accepted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invitation has already been accepted.",
        )
    if invitation.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invitation has been revoked.",
        )
    inv_expires = invitation.expires_at
    if inv_expires.tzinfo is None:
        inv_expires = inv_expires.replace(tzinfo=timezone.utc)
    if inv_expires < now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invitation has expired.",
        )

    # Enforce verified email requirement
    if not principal.user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User email must be verified to accept an invitation.",
        )

    # Enforce normalized email matching: caller's email must match invitation
    caller_email = principal.user.email.strip().casefold()
    if invitation.normalized_email != caller_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invitation email does not match authenticated user email.",
        )

    try:
        existing_membership = (
            db.query(OrganizationMembership)
            .filter_by(user_id=principal.user.id, organization_id=invitation.organization_id)
            .one_or_none()
        )
        if not existing_membership:
            membership = OrganizationMembership(
                user_id=principal.user.id,
                organization_id=invitation.organization_id,
                role=invitation.role,
            )
            db.add(membership)

        invitation.accepted_at = now
        db.add(
            AuditEvent(
                actor_user_id=principal.user.id,
                organization_id=invitation.organization_id,
                event_type="invitation.accepted",
                target_type="invitation",
                target_id=str(invitation.id),
                created_at=now,
                metadata_json={"role": invitation.role},
            )
        )
        db.commit()

        org = db.get(Organization, invitation.organization_id)
        return {
            "id": str(org.id),
            "name": org.name if org else "",
            "role": invitation.role,
        }
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to accept invitation.",
        ) from exc


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    db: Session = Depends(get_db),
):
    try:
        db.add(
            AuditEvent(
                actor_user_id=principal.user.id,
                event_type="auth.logout",
                target_type="user",
                target_id=str(principal.user.id),
                auth_time=principal.auth_time,
                created_at=datetime.now(timezone.utc),
                metadata_json={},
            )
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
    return None
