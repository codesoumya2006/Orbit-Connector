from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from typing import Any

import firebase_admin
from firebase_admin import auth
from google.auth.exceptions import DefaultCredentialsError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.entities import AuditEvent, User


_bearer_scheme = HTTPBearer(auto_error=False)
_firebase_app: firebase_admin.App | None = None


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    uid: str
    auth_time: datetime
    user: User


def _get_firebase_app() -> firebase_admin.App:
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app
    if not settings.firebase_project_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service is not configured.",
        )

    credential = (
        firebase_admin.credentials.Certificate(settings.firebase_credentials_path)
        if settings.firebase_credentials_path
        else None
    )

    def initialize_app() -> firebase_admin.App:
        options = {"projectId": settings.firebase_project_id}
        if credential is not None:
            return firebase_admin.initialize_app(credential, options=options)
        return firebase_admin.initialize_app(options=options)

    get_app = getattr(firebase_admin, "get_app", None)
    if get_app is None:
        _firebase_app = initialize_app()
    else:
        try:
            _firebase_app = get_app()
        except ValueError:
            try:
                _firebase_app = initialize_app()
            except ValueError:
                _firebase_app = get_app()
    return _firebase_app


def verify_firebase_id_token(id_token: str) -> dict[str, Any]:
    if not settings.firebase_project_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service is not configured.",
        )

    try:
        _get_firebase_app()
        try:
            claims = auth.verify_id_token(id_token, check_revoked=True)
        except DefaultCredentialsError:
            claims = auth.verify_id_token(id_token, check_revoked=False)
    except (
        auth.InvalidIdTokenError,
        auth.ExpiredIdTokenError,
        auth.RevokedIdTokenError,
        auth.UserDisabledError,
        ValueError,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except firebase_admin.exceptions.FirebaseError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service is unavailable.",
        ) from None
    except DefaultCredentialsError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Firebase Admin credentials are not configured.",
        ) from None
    except HTTPException:
        raise

    uid = claims.get("uid")
    auth_time = claims.get("auth_time")
    if (
        not isinstance(uid, str)
        or not uid
        or not isinstance(auth_time, (int, float))
        or isinstance(auth_time, bool)
        or claims.get("aud") != settings.firebase_project_id
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return claims


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication credentials are required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _resolve_user(db: Session, claims: dict[str, Any]) -> User:
    uid = claims["uid"]
    user = db.query(User).filter_by(firebase_uid=uid).one_or_none()
    if user is not None:
        return user

    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    auth_time = datetime.fromtimestamp(claims["auth_time"], tz=timezone.utc)
    user = User(
        firebase_uid=uid,
        email=email.strip().casefold(),
        email_verified=claims.get("email_verified") is True,
        display_name=claims.get("name")
        if isinstance(claims.get("name"), str)
        else None,
    )
    try:
        db.add(user)
        db.flush()
        db.add(
            AuditEvent(
                actor_user_id=user.id,
                event_type="auth.first_observed",
                target_type="user",
                target_id=str(user.id),
                auth_time=auth_time,
                metadata_json={},
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
        db.refresh(user)
        return user
    except SQLAlchemyError:
        db.rollback()
        existing_user = db.query(User).filter_by(firebase_uid=uid).one_or_none()
        if existing_user is not None:
            return existing_user
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to resolve authenticated user.",
        ) from None


def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthenticatedPrincipal:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise _unauthorized()

    token = credentials.credentials
    if not token or any(character.isspace() for character in token):
        raise _unauthorized()

    claims = verify_firebase_id_token(token)
    user = _resolve_user(db, claims)
    return AuthenticatedPrincipal(
        uid=claims["uid"],
        auth_time=datetime.fromtimestamp(claims["auth_time"], tz=timezone.utc),
        user=user,
    )
