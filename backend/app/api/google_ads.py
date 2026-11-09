from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
    GoogleAdsCampaignReport,
    IntegrationConnection,
    OAuthState,
)
from app.providers.google_ads.constants import GOOGLE_ADS_SCOPE
from app.providers.google_ads.customer import (
    GoogleAdsCustomerError,
    clean_customer_id,
    discover_accessible_customers,
    validate_customer_access,
)
from app.providers.google_ads.oauth import (
    GoogleAdsOAuthError,
    build_google_ads_authorization_url,
    exchange_authorization_code,
    generate_pkce,
    refresh_access_token,
    revoke_token,
)
from app.providers.google_ads.reports import cost_micros_to_decimal
from app.schemas.google_ads import (
    GoogleAdsCampaignReportRowSchema,
    GoogleAdsCustomer,
    GoogleAdsCustomerListResponse,
    GoogleAdsCustomerSelectRequest,
    GoogleAdsDisconnectResponse,
    GoogleAdsOAuthAuthorizeResponse,
    GoogleAdsReportResponse,
    GoogleAdsReportSummary,
)

router = APIRouter()


@router.post("/oauth/google-ads/authorize", response_model=GoogleAdsOAuthAuthorizeResponse)
def initiate_google_ads_oauth(
    connection_id: Optional[str] = Query(None),
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Initiate Google Ads OAuth 2.0 PKCE flow.

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
        if conn.provider_key != "google_ads":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Connection is not a Google Ads provider.",
            )
    else:
        conn = (
            db.query(IntegrationConnection)
            .filter_by(
                organization_id=context.organization_id,
                provider_key="google_ads",
            )
            .first()
        )
        if not conn:
            conn = IntegrationConnection(
                id=str(uuid4()),
                organization_id=context.organization_id,
                provider_key="google_ads",
                display_name="Google Ads",
                status="needs_authorization",
                connection_type="oauth",
                config_json={},
                created_at=now,
            )
            db.add(conn)
            db.flush()

    # 2. Generate PKCE pair and state
    verifier, challenge = generate_pkce()
    state_token = secrets.token_urlsafe(32)

    # 3. Store state in database with encrypted verifier and short expiration (10 min)
    encrypted_verifier = encrypt_secret(verifier)
    oauth_state = OAuthState(
        state_token=state_token,
        code_verifier=encrypted_verifier,
        organization_id=context.organization_id,
        user_id=context.user_id,
        connection_id=conn.id,
        expires_at=now + timedelta(minutes=10),
        used_at=None,
        created_at=now,
    )
    db.add(oauth_state)
    db.commit()

    # 4. Build authorization URL
    auth_url = build_google_ads_authorization_url(
        state=state_token,
        code_challenge=challenge,
    )

    return GoogleAdsOAuthAuthorizeResponse(
        authorization_url=auth_url,
        state=state_token,
        connection_id=conn.id,
    )


@router.get("/oauth/google-ads/callback")
def google_ads_oauth_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Handle Google Ads OAuth 2.0 redirect callback.

    Verifies state token atomically, exchanges code for refresh token,
    and updates the connection status.
    """
    now = datetime.now(timezone.utc)

    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google Ads OAuth authorization was denied or failed: {error}",
        )

    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required OAuth 'code' or 'state' parameter.",
        )

    # Validate state token atomically
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

    # Decrypt PKCE code verifier
    raw_verifier = oauth_state.code_verifier
    try:
        decrypted_verifier = decrypt_secret(raw_verifier)
        code_verifier = decrypted_verifier if decrypted_verifier else raw_verifier
    except Exception:
        code_verifier = raw_verifier

    saved_connection_id = oauth_state.connection_id
    saved_org_id = oauth_state.organization_id
    saved_user_id = oauth_state.user_id

    # Mark state as consumed and commit immediately before network call
    oauth_state.used_at = now
    db.commit()

    # Exchange code for tokens
    try:
        tokens = exchange_authorization_code(
            code=code,
            code_verifier=code_verifier,
        )
    except GoogleAdsOAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    # Load connection in fresh transaction
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

    refresh_token = tokens.get("refresh_token")
    existing_config = dict(conn.config_json or {})

    if refresh_token:
        encrypted_rt = encrypt_secret(refresh_token)
        existing_config["refresh_token_encrypted"] = encrypted_rt
    else:
        if not existing_config.get("refresh_token_encrypted"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Google Ads OAuth did not return a refresh token and no prior token exists. Re-authorization with consent is required.",
            )

    existing_config["authorized_at"] = now.isoformat()
    existing_config["scopes"] = [GOOGLE_ADS_SCOPE]

    conn.config_json = existing_config
    if existing_config.get("customer_id"):
        conn.status = "connected"
    else:
        conn.status = "needs_customer_selection"

    audit_entry = AuditEvent(
        id=uuid4(),
        organization_id=saved_org_id,
        actor_user_id=saved_user_id,
        event_type="connector.updated",
        target_type="integration_connection",
        target_id=str(conn.id),
        metadata_json={
            "provider_key": "google_ads",
            "action": "oauth_authorized",
            "status": conn.status,
            "has_customer": bool(existing_config.get("customer_id")),
        },
        created_at=now,
    )
    db.add(audit_entry)
    db.commit()

    frontend_base = settings.frontend_origin or "http://localhost:3000"
    redirect_url = f"{frontend_base}/?google_ads_connected=true&connection_id={conn.id}"
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)


@router.get(
    "/connections/{connection_id}/google-ads/customers",
    response_model=GoogleAdsCustomerListResponse,
)
def list_google_ads_customers(
    connection_id: str,
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    """List accessible Google Ads customer accounts (including linked manager clients).

    Requires Viewer, Member, Admin, or Owner role.
    """
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
    if conn.provider_key != "google_ads":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connection is not a Google Ads integration.",
        )

    config = conn.config_json or {}
    encrypted_rt = config.get("refresh_token_encrypted")
    if not encrypted_rt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google Ads connection is not authorized. Please complete OAuth authorization first.",
        )

    try:
        refresh_token = decrypt_secret(encrypted_rt)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decrypt connection credentials.",
        ) from exc

    try:
        access_token = refresh_access_token(refresh_token)
    except GoogleAdsOAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Failed to refresh Google Ads access token: {exc}",
        )

    developer_token = settings.google_ads_developer_token
    if not developer_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GOOGLE_ADS_DEVELOPER_TOKEN is not configured in backend environment.",
        )

    try:
        discovered = discover_accessible_customers(
            access_token=access_token,
            developer_token=developer_token,
        )
    except GoogleAdsCustomerError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    customers = [
        GoogleAdsCustomer(
            customer_id=c["customer_id"],
            descriptive_name=c["descriptive_name"],
            is_manager=c.get("is_manager", False),
            currency_code=c.get("currency_code"),
            time_zone=c.get("time_zone"),
            login_customer_id=c.get("login_customer_id"),
            test_account=c.get("test_account", False),
        )
        for c in discovered
    ]
    return GoogleAdsCustomerListResponse(customers=customers)


@router.post("/connections/{connection_id}/google-ads/customers/select")
def select_google_ads_customer(
    connection_id: str,
    payload: GoogleAdsCustomerSelectRequest,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Select a specific Google Ads customer account to sync with authorization validation.

    Requires Owner or Admin role.
    """
    now = datetime.now(timezone.utc)
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
    if conn.provider_key != "google_ads":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connection is not a Google Ads integration.",
        )

    config = dict(conn.config_json or {})
    encrypted_rt = config.get("refresh_token_encrypted")
    if not encrypted_rt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot select customer: connection has not completed OAuth authorization.",
        )

    developer_token = settings.google_ads_developer_token
    if not developer_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GOOGLE_ADS_DEVELOPER_TOKEN is not configured in backend environment.",
        )

    try:
        refresh_token = decrypt_secret(encrypted_rt)
        access_token = refresh_access_token(refresh_token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authorization failure refreshing Google Ads credentials: {exc}",
        )

    # Validate customer access and hierarchy (never trust client-supplied login-customer-id blindly)
    try:
        verified_details = validate_customer_access(
            access_token=access_token,
            developer_token=developer_token,
            target_customer_id=payload.customer_id,
            target_login_customer_id=payload.login_customer_id,
        )
    except GoogleAdsCustomerError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    cid = verified_details["customer_id"]
    cname = payload.customer_name or verified_details.get("descriptive_name") or f"Account {cid}"
    login_cid = verified_details.get("login_customer_id")
    currency_code = verified_details.get("currency_code") or "USD"
    time_zone = verified_details.get("time_zone")

    config["customer_id"] = cid
    config["customer_name"] = cname
    config["login_customer_id"] = login_cid
    config["currency_code"] = currency_code
    config["time_zone"] = time_zone
    config["is_manager"] = verified_details.get("is_manager", False)

    conn.config_json = config
    conn.status = "connected"

    audit_entry = AuditEvent(
        id=uuid4(),
        organization_id=context.organization_id,
        actor_user_id=context.user_id,
        event_type="connector.updated",
        target_type="integration_connection",
        target_id=str(conn.id),
        metadata_json={
            "provider_key": "google_ads",
            "action": "select_customer",
            "customer_id": cid,
            "customer_name": cname,
            "login_customer_id": login_cid,
            "currency_code": currency_code,
        },
        created_at=now,
    )
    db.add(audit_entry)
    db.commit()

    return {
        "status": "success",
        "connection_id": conn.id,
        "customer_id": cid,
        "customer_name": cname,
        "login_customer_id": login_cid,
        "currency_code": currency_code,
    }


@router.get(
    "/connections/{connection_id}/google-ads/reports",
    response_model=GoogleAdsReportResponse,
)
def get_google_ads_reports(
    connection_id: str,
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    campaign_id: Optional[str] = Query(None),
    customer_id: Optional[str] = Query(None),
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    """Retrieve persisted Google Ads campaign report data for this connection.

    Requires Viewer, Member, Admin, or Owner role.
    """
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
    if conn.provider_key != "google_ads":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connection is not a Google Ads integration.",
        )

    configured_customer_id = (conn.config_json or {}).get("customer_id")
    if customer_id and configured_customer_id and customer_id != configured_customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Requested customer_id does not match the configured connection customer.",
        )

    query = db.query(GoogleAdsCampaignReport).filter_by(
        organization_id=context.organization_id,
        connection_id=connection_id,
    )
    if customer_id:
        query = query.filter(GoogleAdsCampaignReport.customer_id == customer_id)
    elif configured_customer_id:
        query = query.filter(GoogleAdsCampaignReport.customer_id == configured_customer_id)

    if start_date:
        query = query.filter(GoogleAdsCampaignReport.report_date >= start_date)
    if end_date:
        query = query.filter(GoogleAdsCampaignReport.report_date <= end_date)
    if campaign_id:
        query = query.filter(GoogleAdsCampaignReport.campaign_id == campaign_id)

    total = query.count()

    # Compute summary totals using exact Decimal financial precision
    all_matching = query.all()
    total_cost_micros = sum(r.cost_micros for r in all_matching)
    total_cost_dec = cost_micros_to_decimal(total_cost_micros)
    total_conv_dec = sum(Decimal(str(r.conversions)) for r in all_matching) if all_matching else Decimal("0.0")
    total_conv_val_dec = sum(Decimal(str(r.conversions_value)) for r in all_matching) if all_matching else Decimal("0.0")

    currency_code = (conn.config_json or {}).get("currency_code") or "USD"

    summary = GoogleAdsReportSummary(
        currency_code=currency_code,
        total_impressions=sum(r.impressions for r in all_matching),
        total_clicks=sum(r.clicks for r in all_matching),
        total_cost_micros=total_cost_micros,
        total_cost=total_cost_dec,
        total_conversions=float(total_conv_dec),
        total_conversions_value=float(total_conv_val_dec),
    )

    rows = (
        query.order_by(
            GoogleAdsCampaignReport.report_date.desc(),
            GoogleAdsCampaignReport.campaign_id.asc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    items = [
        GoogleAdsCampaignReportRowSchema(
            id=r.id,
            organization_id=r.organization_id,
            connection_id=r.connection_id,
            customer_id=r.customer_id,
            report_date=r.report_date,
            campaign_id=r.campaign_id,
            campaign_name=r.campaign_name,
            campaign_status=r.campaign_status,
            impressions=r.impressions,
            clicks=r.clicks,
            cost_micros=r.cost_micros,
            cost=r.cost,
            conversions=r.conversions,
            conversions_value=r.conversions_value,
            sync_job_id=r.sync_job_id,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]

    return GoogleAdsReportResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        summary=summary,
    )


@router.post(
    "/connections/{connection_id}/google-ads/disconnect",
    response_model=GoogleAdsDisconnectResponse,
)
def disconnect_google_ads(
    connection_id: str,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Disconnect Google Ads integration and purge stored credentials.

    Requires Owner or Admin role.
    """
    now = datetime.now(timezone.utc)
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
    if conn.provider_key != "google_ads":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connection is not a Google Ads integration.",
        )

    config = dict(conn.config_json or {})
    encrypted_rt = config.get("refresh_token_encrypted")
    if encrypted_rt:
        try:
            refresh_token = decrypt_secret(encrypted_rt)
            if refresh_token:
                revoke_token(refresh_token)
        except Exception:
            pass

    conn.config_json = {}
    conn.status = "disconnected"

    audit_entry = AuditEvent(
        id=uuid4(),
        organization_id=context.organization_id,
        actor_user_id=context.user_id,
        event_type="connector.deleted",
        target_type="integration_connection",
        target_id=str(conn.id),
        metadata_json={
            "provider_key": "google_ads",
            "action": "disconnected",
        },
        created_at=now,
    )
    db.add(audit_entry)
    db.commit()

    return GoogleAdsDisconnectResponse(status="disconnected", disconnected=True)
