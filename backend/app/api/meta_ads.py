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
    IntegrationConnection,
    MetaAdsCampaignReport,
    OAuthState,
)
from app.providers.meta_ads.account import (
    MetaAdsAccountError,
    clean_ad_account_id,
    list_accessible_ad_accounts,
    validate_ad_account_access,
)
from app.providers.meta_ads.oauth import (
    MetaAdsOAuthError,
    build_meta_ads_authorization_url,
    exchange_authorization_code,
    exchange_for_long_lived_token,
    revoke_meta_token,
)
from app.schemas.meta_ads import (
    MetaAdsAccount,
    MetaAdsAccountListResponse,
    MetaAdsAccountSelectRequest,
    MetaAdsCampaignReportRowSchema,
    MetaAdsDisconnectResponse,
    MetaAdsOAuthAuthorizeResponse,
    MetaAdsReportResponse,
    MetaAdsReportSummary,
)

router = APIRouter()


@router.post("/oauth/meta-ads/authorize", response_model=MetaAdsOAuthAuthorizeResponse)
def initiate_meta_ads_oauth(
    connection_id: Optional[str] = Query(None),
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Initiate Meta Ads OAuth 2.0 authorization code flow.

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
        if conn.provider_key != "meta_ads":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Connection is not a Meta Ads provider.",
            )
    else:
        conn = (
            db.query(IntegrationConnection)
            .filter_by(
                organization_id=context.organization_id,
                provider_key="meta_ads",
            )
            .first()
        )
        if not conn:
            conn = IntegrationConnection(
                id=str(uuid4()),
                organization_id=context.organization_id,
                provider_key="meta_ads",
                display_name="Meta Ads",
                status="needs_authorization",
                connection_type="oauth",
                config_json={},
                created_at=now,
            )
            db.add(conn)
            db.flush()

    # 2. Generate cryptographically random, single-use state token
    state_token = secrets.token_urlsafe(32)
    oauth_state = OAuthState(
        state_token=state_token,
        code_verifier=encrypt_secret("meta_oauth_placeholder_verifier"),
        organization_id=context.organization_id,
        user_id=context.user_id,
        connection_id=conn.id,
        expires_at=now + timedelta(minutes=10),
        created_at=now,
    )
    db.add(oauth_state)
    db.commit()

    # 3. Build authorization URL
    auth_url = build_meta_ads_authorization_url(state=state_token)

    return MetaAdsOAuthAuthorizeResponse(
        authorization_url=auth_url,
        state=state_token,
        connection_id=conn.id,
    )


@router.get("/oauth/meta-ads/callback")
def meta_ads_oauth_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Handle Meta OAuth 2.0 callback, exchange code for long-lived token, and persist encrypted credentials."""
    frontend_base = settings.frontend_origin.rstrip("/")
    now = datetime.now(timezone.utc)

    if error:
        err_msg = error_description or error
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Meta Ads OAuth authorization failed: {err_msg}",
        )

    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required OAuth 'code' or 'state' parameter.",
        )

    # 1. Atomic state verification and consumption
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

    expires_at = oauth_state.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at < now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth state token has expired.",
        )

    conn_id = oauth_state.connection_id
    org_id = oauth_state.organization_id
    user_id = oauth_state.user_id

    # Consume state atomically before external HTTP calls
    oauth_state.used_at = now
    db.commit()

    conn = db.query(IntegrationConnection).filter_by(id=conn_id, organization_id=org_id).first()
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Associated integration connection not found.",
        )

    # 2. Exchange authorization code for short-lived token, then long-lived token (~60 days)
    try:
        token_data = exchange_authorization_code(code)
        short_token = token_data["access_token"]
        long_lived_data = exchange_for_long_lived_token(short_token)
        long_lived_token = long_lived_data["access_token"]
        expires_in = long_lived_data.get("expires_in", 5184000)
    except MetaAdsOAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    # 3. Encrypt access token and update connection
    config = dict(conn.config_json or {})
    config["access_token_encrypted"] = encrypt_secret(long_lived_token)
    config["token_expires_at"] = (now + timedelta(seconds=expires_in)).isoformat()
    config["authorized_at"] = now.isoformat()

    conn.config_json = config
    if config.get("ad_account_id"):
        conn.status = "connected"
    else:
        conn.status = "needs_account_selection"

    audit_entry = AuditEvent(
        id=uuid4(),
        organization_id=org_id,
        actor_user_id=user_id,
        event_type="connector.authorized",
        target_type="integration_connection",
        target_id=str(conn.id),
        metadata_json={
            "provider_key": "meta_ads",
            "connection_id": str(conn.id),
            "status": conn.status,
        },
        created_at=now,
    )
    db.add(audit_entry)
    db.commit()

    redirect_url = f"{frontend_base}/?meta_ads_connected=true&connection_id={conn.id}"
    return RedirectResponse(
        url=redirect_url,
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


@router.get(
    "/connections/{connection_id}/meta-ads/accounts",
    response_model=MetaAdsAccountListResponse,
)
def list_meta_ad_accounts(
    connection_id: str,
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    """List Meta ad accounts accessible to the authenticated connection.

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
    if conn.provider_key != "meta_ads":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connection is not a Meta Ads integration.",
        )

    config = conn.config_json or {}
    encrypted_token = config.get("access_token_encrypted")
    if not encrypted_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connection has not completed Meta OAuth authorization.",
        )

    try:
        access_token = decrypt_secret(encrypted_token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to decrypt stored credentials: {exc}",
        )

    try:
        raw_accounts = list_accessible_ad_accounts(access_token)
    except MetaAdsAccountError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    accounts = [
        MetaAdsAccount(
            ad_account_id=a["ad_account_id"],
            name=a["name"],
            account_status=a["account_status"],
            currency=a["currency"],
            timezone_name=a["timezone_name"],
            business_name=a.get("business_name"),
            is_prepay=a.get("is_prepay", False),
        )
        for a in raw_accounts
    ]
    return MetaAdsAccountListResponse(accounts=accounts)


@router.post("/connections/{connection_id}/meta-ads/accounts/select")
def select_meta_ad_account(
    connection_id: str,
    payload: MetaAdsAccountSelectRequest,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Select a specific Meta ad account to sync with authorization validation.

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
    if conn.provider_key != "meta_ads":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connection is not a Meta Ads integration.",
        )

    config = dict(conn.config_json or {})
    encrypted_token = config.get("access_token_encrypted")
    if not encrypted_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot select account: connection has not completed OAuth authorization.",
        )

    try:
        access_token = decrypt_secret(encrypted_token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to decrypt stored credentials: {exc}",
        )

    # Server-side validation of account access
    try:
        account_details = validate_ad_account_access(access_token, payload.ad_account_id)
    except MetaAdsAccountError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    cid = account_details["ad_account_id"]
    cname = payload.name or account_details.get("name") or f"Ad Account {cid}"

    raw_currency = account_details.get("currency")
    if not raw_currency or not str(raw_currency).strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Selected Meta ad account '{cid}' has no currency metadata configured.",
        )
    account_currency = str(raw_currency).strip().upper()

    config["ad_account_id"] = cid
    config["ad_account_name"] = cname
    config["currency"] = account_currency
    config["timezone_name"] = account_details.get("timezone_name") or "UTC"
    config["account_status"] = account_details.get("account_status", 1)

    conn.config_json = config
    conn.status = "connected"

    audit_entry = AuditEvent(
        id=uuid4(),
        organization_id=context.organization_id,
        actor_user_id=context.user_id,
        event_type="connector.configured",
        target_type="integration_connection",
        target_id=str(conn.id),
        metadata_json={
            "provider_key": "meta_ads",
            "ad_account_id": cid,
            "ad_account_name": cname,
            "currency": config["currency"],
        },
        created_at=now,
    )
    db.add(audit_entry)
    db.commit()

    return {
        "status": "connected",
        "ad_account_id": cid,
        "ad_account_name": cname,
        "currency": config["currency"],
    }


@router.get(
    "/connections/{connection_id}/meta-ads/reports",
    response_model=MetaAdsReportResponse,
)
def get_meta_ads_reports(
    connection_id: str,
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    campaign_id: Optional[str] = Query(None),
    ad_account_id: Optional[str] = Query(None),
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    """Retrieve persisted Meta Ads campaign report data for this connection.

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
    if conn.provider_key != "meta_ads":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connection is not a Meta Ads integration.",
        )

    configured_account_id = (conn.config_json or {}).get("ad_account_id")
    if ad_account_id and configured_account_id and clean_ad_account_id(ad_account_id) != configured_account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Requested ad_account_id does not match the configured connection account.",
        )

    query = db.query(MetaAdsCampaignReport).filter_by(
        organization_id=context.organization_id,
        connection_id=connection_id,
    )

    if ad_account_id:
        query = query.filter(MetaAdsCampaignReport.ad_account_id == clean_ad_account_id(ad_account_id))
    elif configured_account_id:
        query = query.filter(MetaAdsCampaignReport.ad_account_id == configured_account_id)

    if start_date:
        query = query.filter(MetaAdsCampaignReport.report_date >= start_date)
    if end_date:
        query = query.filter(MetaAdsCampaignReport.report_date <= end_date)
    if campaign_id:
        query = query.filter(MetaAdsCampaignReport.campaign_id == campaign_id)

    total = query.count()

    all_matching = query.all()
    total_spend = sum((r.spend for r in all_matching), Decimal("0.000000"))
    total_conversions = sum(r.conversions for r in all_matching)
    total_conv_val = sum((r.conversions_value for r in all_matching), Decimal("0.000000"))

    currency_code = (conn.config_json or {}).get("currency")
    if not currency_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Integration connection is missing currency metadata. Please re-select the ad account.",
        )

    summary = MetaAdsReportSummary(
        currency=currency_code,
        currency_code=currency_code,
        total_impressions=sum(r.impressions for r in all_matching),
        total_reach=sum(r.reach for r in all_matching),
        total_clicks=sum(r.clicks for r in all_matching),
        total_spend=total_spend,
        total_conversions=float(total_conversions),
        total_conversions_value=total_conv_val,
    )

    rows = (
        query.order_by(
            MetaAdsCampaignReport.report_date.desc(),
            MetaAdsCampaignReport.campaign_id.asc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    items = [
        MetaAdsCampaignReportRowSchema(
            id=r.id,
            organization_id=r.organization_id,
            connection_id=r.connection_id,
            ad_account_id=r.ad_account_id,
            report_date=r.report_date,
            campaign_id=r.campaign_id,
            campaign_name=r.campaign_name,
            campaign_status=r.campaign_status,
            impressions=r.impressions,
            reach=r.reach,
            clicks=r.clicks,
            spend=r.spend,
            ctr=r.ctr,
            cpc=r.cpc,
            cpm=r.cpm,
            frequency=r.frequency,
            conversions=r.conversions,
            conversions_value=r.conversions_value,
            sync_job_id=r.sync_job_id,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]

    return MetaAdsReportResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        summary=summary,
    )


@router.post(
    "/connections/{connection_id}/meta-ads/disconnect",
    response_model=MetaAdsDisconnectResponse,
)
def disconnect_meta_ads(
    connection_id: str,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Disconnect Meta Ads integration and purge stored credentials.

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
    if conn.provider_key != "meta_ads":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connection is not a Meta Ads integration.",
        )

    config = dict(conn.config_json or {})
    encrypted_token = config.get("access_token_encrypted")
    if encrypted_token:
        try:
            token = decrypt_secret(encrypted_token)
            if token:
                revoke_meta_token(token)
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
            "provider_key": "meta_ads",
            "action": "disconnected",
        },
        created_at=now,
    )
    db.add(audit_entry)
    db.commit()

    return MetaAdsDisconnectResponse(status="disconnected", disconnected=True)

