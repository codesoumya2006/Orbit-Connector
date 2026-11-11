from datetime import datetime, timedelta, timezone
import secrets
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.context import OrganizationContext, require_permission
from app.core.database import get_db
from app.core.security import decrypt_secret, encrypt_secret
from app.models.entities import (
    AuditEvent,
    GA4ReportRow,
    IntegrationConnection,
    OAuthState,
)
from app.providers.google_analytics.admin import (
    GoogleAnalyticsAdminError,
    list_accounts_and_properties,
    verify_property_accessible,
)
from app.providers.google_analytics.oauth import (
    GA4_READONLY_SCOPE,
    GoogleOAuthError,
    build_google_authorization_url,
    exchange_authorization_code,
    generate_pkce,
    refresh_access_token,
)
from app.schemas.google_analytics import (
    GA4ReportResponse,
    GA4ReportRowSchema,
    OAuthAuthorizeResponse,
    PropertyItem,
    PropertySelectRequest,
)

router = APIRouter()


@router.post("/oauth/google/authorize", response_model=OAuthAuthorizeResponse)
def initiate_google_oauth(
    connection_id: Optional[str] = Query(None),
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Initiate Google OAuth 2.0 PKCE flow for a Google Analytics connection.

    Requires Owner or Admin role.
    """
    now = datetime.now(timezone.utc)

    # 1. Resolve or create integration connection
    if connection_id:
        conn = (
            db.query(IntegrationConnection)
            .filter_by(id=connection_id, organization_id=context.organization_id)
            .first()
        )
        if not conn:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Integration connection not found.",
            )
        if conn.provider_key != "google_analytics":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Connection is not a Google Analytics provider.",
            )
    else:
        conn = (
            db.query(IntegrationConnection)
            .filter_by(
                organization_id=context.organization_id,
                provider_key="google_analytics",
            )
            .first()
        )
        if not conn:
            conn = IntegrationConnection(
                id=str(uuid4()),
                organization_id=context.organization_id,
                provider_key="google_analytics",
                display_name="Google Analytics 4",
                status="needs_authorization",
                connection_type="oauth",
                config_json={},
                created_at=now,
            )
            db.add(conn)
            db.flush()

    # 2. Generate PKCE pair, state, and build authorization URL before committing OAuth state
    verifier, challenge = generate_pkce()
    state_token = secrets.token_urlsafe(32)

    try:
        auth_url = build_google_authorization_url(state=state_token, code_challenge=challenge)
    except GoogleOAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    # 3. Store state in database with encrypted verifier and short expiration (10 min)
    encrypted_verifier = encrypt_secret(verifier)
    oauth_state = OAuthState(
        state_token=state_token,
        code_verifier=encrypted_verifier,
        organization_id=context.organization_id,
        user_id=context.user_id,
        connection_id=conn.id,
        expires_at=now + timedelta(minutes=10),
        created_at=now,
    )
    db.add(oauth_state)
    db.commit()

    return OAuthAuthorizeResponse(
        authorization_url=auth_url,
        state=state_token,
        connection_id=conn.id,
    )


@router.get("/oauth/google/callback")
def handle_google_oauth_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Handle Google OAuth 2.0 callback, exchange code for tokens, and store encrypted refresh token."""
    now = datetime.now(timezone.utc)

    # 1. Handle user/provider denial
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google OAuth authorization was denied or failed: {error}",
        )

    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required OAuth 'code' or 'state' parameter.",
        )

    # 2. Validate state token atomically in a short transaction
    query = (
        db.query(OAuthState)
        .filter_by(state_token=state)
        .execution_options(populate_existing=True)
    )
    try:
        oauth_state = query.with_for_update().first()
    except Exception:
        oauth_state = query.first()

    if not oauth_state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or unrecognized OAuth state token.",
        )

    if oauth_state.used_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth state token has already been consumed (replay rejected).",
        )

    exp = oauth_state.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth state token has expired.",
        )

    # Decrypt PKCE code verifier (supports encrypted and legacy plaintext gracefully)
    raw_verifier = oauth_state.code_verifier
    try:
        decrypted_verifier = decrypt_secret(raw_verifier)
        code_verifier = decrypted_verifier if decrypted_verifier else raw_verifier
    except Exception:
        code_verifier = raw_verifier

    saved_connection_id = oauth_state.connection_id
    saved_org_id = oauth_state.organization_id
    saved_user_id = oauth_state.user_id

    # 3. Mark state as consumed and commit immediately to release DB lock before network request
    oauth_state.used_at = now
    db.commit()

    # 4. Exchange code on backend (no DB locks or transaction held during external network request)
    try:
        tokens = exchange_authorization_code(
            code=code,
            code_verifier=code_verifier,
        )
    except GoogleOAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    # 5. Load connection in fresh transaction
    conn = (
        db.query(IntegrationConnection)
        .filter_by(
            id=saved_connection_id,
            organization_id=saved_org_id,
        )
        .first()
    )
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Associated integration connection not found.",
        )

    # 6. Extract and encrypt refresh token
    refresh_token = tokens.get("refresh_token")
    existing_config = dict(conn.config_json or {})

    if refresh_token:
        encrypted_rt = encrypt_secret(refresh_token)
        existing_config["refresh_token_encrypted"] = encrypted_rt
    else:
        # If Google didn't return a refresh token, verify if we already have one stored
        if not existing_config.get("refresh_token_encrypted"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Google OAuth did not return a refresh token and no prior token exists. Re-authorization with consent is required.",
            )

    existing_config["authorized_at"] = now.isoformat()
    existing_config["scopes"] = [GA4_READONLY_SCOPE]

    conn.config_json = existing_config
    if existing_config.get("property_id"):
        conn.status = "connected"
    else:
        conn.status = "needs_configuration"

    # 7. Audit event (strictly sanitized)
    audit = AuditEvent(
        actor_user_id=saved_user_id,
        organization_id=saved_org_id,
        event_type="connection.oauth_authorized",
        target_type="connection",
        target_id=conn.id,
        created_at=now,
        metadata_json={
            "provider_key": conn.provider_key,
            "status": conn.status,
            "scopes": [GA4_READONLY_SCOPE],
        },
    )
    try:
        db.add(audit)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist integration credentials and audit event.",
        ) from exc

    # 8. Return response (Redirect to frontend or JSON based on Accept header)
    accept_header = request.headers.get("accept", "")
    if "application/json" in accept_header and "text/html" not in accept_header:
        return {
            "status": "ok",
            "connection_id": conn.id,
            "connection_status": conn.status,
        }

    redirect_url = f"{settings.frontend_origin}/?connection_id={conn.id}&status=needs_configuration"
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@router.get(
    "/connections/{connection_id}/google-analytics/properties",
    response_model=list[PropertyItem],
)
def list_properties(
    connection_id: str,
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    """List accessible Google Analytics 4 accounts and properties for the connection."""
    conn = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Integration connection not found.",
        )
    if conn.provider_key != "google_analytics":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connection is not a Google Analytics provider.",
        )

    config = conn.config_json or {}
    encrypted_rt = config.get("refresh_token_encrypted")
    if not encrypted_rt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connection is not yet authorized with Google OAuth.",
        )

    try:
        refresh_token = decrypt_secret(encrypted_rt)
        if not refresh_token:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to decrypt stored credentials.",
            )
        access_token = refresh_access_token(refresh_token)
        discovered = list_accounts_and_properties(access_token)
        return [
            PropertyItem(
                property_id=p["property_id"],
                display_name=p["display_name"],
                account_id=p["account_id"],
                account_name=p["account_name"],
            )
            for p in discovered
        ]
    except GoogleOAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Google authentication error: {exc}",
        )
    except GoogleAnalyticsAdminError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Google Analytics Admin API error: {exc}",
        )


@router.post(
    "/connections/{connection_id}/google-analytics/properties/select",
)
def select_property(
    connection_id: str,
    payload: PropertySelectRequest,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Select and validate an accessible GA4 property for the connection."""
    conn = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Integration connection not found.",
        )
    if conn.provider_key != "google_analytics":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connection is not a Google Analytics provider.",
        )

    config = conn.config_json or {}
    encrypted_rt = config.get("refresh_token_encrypted")
    if not encrypted_rt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connection is not yet authorized with Google OAuth.",
        )

    # 1. Obtain access token and verify property accessibility
    try:
        refresh_token = decrypt_secret(encrypted_rt)
        if not refresh_token:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to decrypt stored credentials.",
            )
        access_token = refresh_access_token(refresh_token)
        verified_prop = verify_property_accessible(access_token, payload.property_id)
        if not verified_prop:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Selected property is not accessible to the connected Google account.",
            )
    except GoogleOAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Google authentication error: {exc}",
        )
    except GoogleAnalyticsAdminError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Google Analytics Admin API error: {exc}",
        )

    # 2. Save verified property metadata into connection
    now = datetime.now(timezone.utc)
    updated_config = dict(config)
    updated_config["property_id"] = verified_prop["property_id"]
    updated_config["property_name"] = verified_prop["display_name"]
    updated_config["account_id"] = verified_prop["account_id"]
    updated_config["account_name"] = verified_prop["account_name"]
    updated_config["configured_at"] = now.isoformat()

    conn.config_json = updated_config
    conn.status = "connected"

    audit = AuditEvent(
        actor_user_id=context.user_id,
        organization_id=context.organization_id,
        event_type="connection.property_selected",
        target_type="connection",
        target_id=conn.id,
        created_at=now,
        metadata_json={
            "property_id": verified_prop["property_id"],
            "property_name": verified_prop["display_name"],
            "account_name": verified_prop["account_name"],
        },
    )
    db.add(audit)
    db.commit()
    db.refresh(conn)

    return {
        "id": conn.id,
        "status": conn.status,
        "property_id": verified_prop["property_id"],
        "property_name": verified_prop["display_name"],
        "account_name": verified_prop["account_name"],
    }


@router.get(
    "/connections/{connection_id}/google-analytics/reports",
    response_model=GA4ReportResponse,
)
def get_reports(
    connection_id: str,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    """Retrieve normalized reporting data for a Google Analytics connection."""
    conn = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Integration connection not found.",
        )

    query = db.query(GA4ReportRow).filter_by(
        organization_id=context.organization_id,
        connection_id=conn.id,
    )

    if start_date:
        query = query.filter(GA4ReportRow.report_date >= start_date)
    if end_date:
        query = query.filter(GA4ReportRow.report_date <= end_date)

    total = query.count()
    items = (
        query.order_by(GA4ReportRow.report_date.desc(), GA4ReportRow.channel_group.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return GA4ReportResponse(
        items=[
            GA4ReportRowSchema(
                id=r.id,
                property_id=r.property_id,
                report_date=r.report_date,
                channel_group=r.channel_group,
                sessions=r.sessions,
                active_users=r.active_users,
                event_count=r.event_count,
                sync_job_id=r.sync_job_id,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in items
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/connections/{connection_id}/disconnect")
def disconnect_connection(
    connection_id: str,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Disconnect Google Analytics connection, removing encrypted credentials and configuration."""
    conn = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Integration connection not found.",
        )

    now = datetime.now(timezone.utc)
    config = dict(conn.config_json or {})
    encrypted_rt = config.pop("refresh_token_encrypted", None)
    config.pop("property_id", None)
    config.pop("property_name", None)
    config.pop("account_id", None)
    config.pop("account_name", None)
    config["disconnected_at"] = now.isoformat()

    # Attempt best-effort remote OAuth token revocation with Google
    if encrypted_rt:
        try:
            refresh_token = decrypt_secret(encrypted_rt)
            if refresh_token:
                import httpx
                try:
                    with httpx.Client(timeout=5.0) as client:
                        client.post("https://oauth2.googleapis.com/revoke", data={"token": refresh_token})
                except Exception:
                    pass  # Non-fatal: local credential removal must proceed
        except Exception:
            pass

    conn.config_json = config
    conn.status = "needs_authorization"

    audit = AuditEvent(
        actor_user_id=context.user_id,
        organization_id=context.organization_id,
        event_type="connection.disconnected",
        target_type="connection",
        target_id=conn.id,
        created_at=now,
        metadata_json={
            "provider_key": conn.provider_key,
            "status": conn.status,
        },
    )
    db.add(audit)
    db.commit()
    db.refresh(conn)

    return {
        "id": conn.id,
        "status": conn.status,
        "message": "Connection disconnected successfully.",
    }

