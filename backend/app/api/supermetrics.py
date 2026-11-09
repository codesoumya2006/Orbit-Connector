from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.context import OrganizationContext, require_permission
from app.core.database import get_db
from app.core.security import decrypt_secret, encrypt_secret
from app.models.entities import (
    AuditEvent,
    IntegrationConnection,
    SupermetricsMarketingReport,
)
from app.providers.supermetrics.client import (
    SupermetricsAuthError,
    SupermetricsDataError,
    SupermetricsError,
    SupermetricsRateLimitError,
    list_supermetrics_accounts,
    list_supermetrics_data_sources,
    list_supermetrics_login_accounts,
    list_supermetrics_logins,
    validate_supermetrics_api_key,
)
from app.schemas.supermetrics import (
    SupermetricsAccount,
    SupermetricsAccountListResponse,
    SupermetricsConfigRequest,
    SupermetricsConfigResponse,
    SupermetricsDataSource,
    SupermetricsDataSourceListResponse,
    SupermetricsDisconnectResponse,
    SupermetricsLogin,
    SupermetricsLoginListResponse,
    SupermetricsReportResponse,
    SupermetricsReportRowSchema,
    SupermetricsReportSummary,
    SupermetricsSourceSelectRequest,
)

router = APIRouter()


@router.post(
    "/connections/{connection_id}/supermetrics/config",
    response_model=SupermetricsConfigResponse,
)
def configure_supermetrics(
    connection_id: str,
    payload: SupermetricsConfigRequest,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Configure or update Supermetrics API key with encrypted persistence and pre-validation.

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
    if conn.provider_key != "supermetrics":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connection is not a Supermetrics integration.",
        )

    # 1. Server-side validation of the API key
    try:
        validate_supermetrics_api_key(api_key=payload.api_key)
    except SupermetricsAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Supermetrics authentication failed: {exc}",
        )
    except SupermetricsRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Supermetrics rate limit reached: {exc}",
        )
    except SupermetricsError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Supermetrics validation error: {exc}",
        )

    # 2. Encrypt credentials at rest
    config = dict(conn.config_json or {})
    config["api_key_encrypted"] = encrypt_secret(payload.api_key.strip())
    config["authorized_at"] = now.isoformat()

    if payload.display_name:
        conn.display_name = payload.display_name

    # If already has a selected data source, remain connected; else prompt source selection
    if config.get("ds_id"):
        conn.status = "connected"
    else:
        conn.status = "needs_source_selection"

    conn.config_json = config

    audit_entry = AuditEvent(
        id=uuid4(),
        organization_id=context.organization_id,
        actor_user_id=context.user_id,
        event_type="connector.configured",
        target_type="integration_connection",
        target_id=str(conn.id),
        metadata_json={
            "provider_key": "supermetrics",
            "action": "configure_api_key",
            "status": conn.status,
            "has_source": bool(config.get("ds_id")),
        },
        created_at=now,
    )
    db.add(audit_entry)
    db.commit()

    return SupermetricsConfigResponse(
        status=conn.status,
        connection_id=conn.id,
        has_secret=True,
        message="Supermetrics API key successfully verified and stored encrypted.",
    )


@router.post("/connections/{connection_id}/supermetrics/test")
def test_supermetrics_connection(
    connection_id: str,
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    """Test connectivity using stored encrypted Supermetrics credentials.

    Requires Viewer, Member, Admin, or Owner role.
    """
    conn = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Integration connection not found.")
    if conn.provider_key != "supermetrics":
        raise HTTPException(status_code=400, detail="Connection is not a Supermetrics integration.")

    config = dict(conn.config_json or {})
    encrypted_key = config.get("api_key_encrypted")
    if not encrypted_key:
        raise HTTPException(status_code=400, detail="Supermetrics connection has no stored credentials.")

    try:
        api_key = decrypt_secret(encrypted_key)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to decrypt stored credentials: {exc}")

    try:
        validate_supermetrics_api_key(api_key=api_key)
        return {"status": "ok", "valid": True, "message": "Supermetrics API key is valid and connected."}
    except SupermetricsAuthError as exc:
        raise HTTPException(status_code=401, detail=f"Supermetrics authentication failed: {exc}")
    except SupermetricsRateLimitError as exc:
        raise HTTPException(status_code=429, detail=f"Supermetrics rate limit: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Supermetrics connection test failed: {exc}")


@router.get(
    "/connections/{connection_id}/supermetrics/sources",
    response_model=SupermetricsDataSourceListResponse,
)
def list_supermetrics_sources(
    connection_id: str,
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    """List available marketing data sources supported on Supermetrics Enterprise v2."""
    conn = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Integration connection not found.")
    if conn.provider_key != "supermetrics":
        raise HTTPException(status_code=400, detail="Connection is not a Supermetrics integration.")

    config = dict(conn.config_json or {})
    encrypted_key = config.get("api_key_encrypted")
    if not encrypted_key:
        raise HTTPException(status_code=400, detail="Connection has not completed Supermetrics configuration.")

    try:
        api_key = decrypt_secret(encrypted_key)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to decrypt stored credentials: {exc}")

    try:
        sources_data = list_supermetrics_data_sources(api_key=api_key)
        sources = [
            SupermetricsDataSource(
                ds_id=s["ds_id"],
                name=s["name"],
                category=s.get("category"),
                description=s.get("description"),
            )
            for s in sources_data
        ]
        return SupermetricsDataSourceListResponse(sources=sources)
    except SupermetricsAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to list Supermetrics sources: {exc}")


@router.get(
    "/connections/{connection_id}/supermetrics/logins",
    response_model=SupermetricsLoginListResponse,
)
def list_supermetrics_source_logins(
    connection_id: str,
    ds_id: Optional[str] = Query(None, max_length=64),
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    """List authenticated logins for the Supermetrics connection, optionally filtered by ds_id.

    GET https://api.supermetrics.com/enterprise/v2/ds/logins
    """
    conn = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Integration connection not found.")
    if conn.provider_key != "supermetrics":
        raise HTTPException(status_code=400, detail="Connection is not a Supermetrics integration.")

    config = dict(conn.config_json or {})
    encrypted_key = config.get("api_key_encrypted")
    if not encrypted_key:
        raise HTTPException(status_code=400, detail="Connection has not completed Supermetrics configuration.")

    try:
        api_key = decrypt_secret(encrypted_key)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to decrypt stored credentials: {exc}")

    try:
        raw_logins = list_supermetrics_logins(api_key=api_key, ds_id=ds_id)
        logins = [
            SupermetricsLogin(
                login_id=l["login_id"],
                ds_id=l.get("ds_id", ds_id or ""),
                username=l.get("username"),
                display_name=l.get("display_name"),
            )
            for l in raw_logins
        ]
        return SupermetricsLoginListResponse(logins=logins)
    except SupermetricsAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to list logins: {exc}")


@router.get(
    "/connections/{connection_id}/supermetrics/logins/{login_id}/accounts",
    response_model=SupermetricsAccountListResponse,
)
def list_supermetrics_accounts_for_login(
    connection_id: str,
    login_id: str,
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    """List accessible accounts under a specific Supermetrics login.

    GET https://api.supermetrics.com/enterprise/v2/ds/login/{login_id}/accounts
    """
    conn = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Integration connection not found.")
    if conn.provider_key != "supermetrics":
        raise HTTPException(status_code=400, detail="Connection is not a Supermetrics integration.")

    config = dict(conn.config_json or {})
    encrypted_key = config.get("api_key_encrypted")
    if not encrypted_key:
        raise HTTPException(status_code=400, detail="Connection has not completed Supermetrics configuration.")

    try:
        api_key = decrypt_secret(encrypted_key)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to decrypt stored credentials: {exc}")

    try:
        raw_accounts = list_supermetrics_login_accounts(api_key=api_key, login_id=login_id)
        accounts = [
            SupermetricsAccount(
                account_id=a["account_id"],
                account_name=a["account_name"],
                ds_id=a.get("ds_id", config.get("ds_id", "")),
                login_id=login_id,
                currency=a.get("currency"),
            )
            for a in raw_accounts
        ]
        return SupermetricsAccountListResponse(accounts=accounts)
    except SupermetricsAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to list accounts for login '{login_id}': {exc}")


@router.get(
    "/connections/{connection_id}/supermetrics/accounts",
    response_model=SupermetricsAccountListResponse,
)
def list_supermetrics_source_accounts(
    connection_id: str,
    ds_id: str = Query(..., min_length=1, max_length=64),
    login_id: Optional[str] = Query(None, max_length=128),
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    """List accessible accounts for a given Supermetrics data source ID using documented login -> accounts sequence."""
    conn = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Integration connection not found.")
    if conn.provider_key != "supermetrics":
        raise HTTPException(status_code=400, detail="Connection is not a Supermetrics integration.")

    config = dict(conn.config_json or {})
    encrypted_key = config.get("api_key_encrypted")
    if not encrypted_key:
        raise HTTPException(status_code=400, detail="Connection has not completed Supermetrics configuration.")

    try:
        api_key = decrypt_secret(encrypted_key)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to decrypt stored credentials: {exc}")

    try:
        raw_accounts = list_supermetrics_accounts(api_key=api_key, ds_id=ds_id, login_id=login_id)
        accounts = [
            SupermetricsAccount(
                account_id=a["account_id"],
                account_name=a["account_name"],
                ds_id=a.get("ds_id", ds_id),
                login_id=a.get("login_id"),
                currency=a.get("currency"),
            )
            for a in raw_accounts
        ]
        return SupermetricsAccountListResponse(accounts=accounts)
    except SupermetricsAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to list accounts for '{ds_id}': {exc}")


@router.post("/connections/{connection_id}/supermetrics/sources/select")
def select_supermetrics_source(
    connection_id: str,
    payload: SupermetricsSourceSelectRequest,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Select and configure target data source and accounts to synchronize."""
    now = datetime.now(timezone.utc)
    conn = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Integration connection not found.")
    if conn.provider_key != "supermetrics":
        raise HTTPException(status_code=400, detail="Connection is not a Supermetrics integration.")

    config = dict(conn.config_json or {})
    if not config.get("api_key_encrypted"):
        raise HTTPException(status_code=400, detail="Connection has no stored credentials.")

    clean_ds = payload.ds_id.strip().upper()
    config["ds_id"] = clean_ds
    config["ds_name"] = payload.ds_name or clean_ds
    config["ds_accounts"] = payload.ds_accounts.strip()
    config["ds_account_name"] = payload.ds_account_name or payload.ds_accounts.strip()
    config["currency"] = (payload.currency or "USD").strip().upper()

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
            "provider_key": "supermetrics",
            "action": "select_source",
            "ds_id": clean_ds,
            "ds_accounts": config["ds_accounts"],
            "currency": config["currency"],
        },
        created_at=now,
    )
    db.add(audit_entry)
    db.commit()

    return {
        "status": "connected",
        "ds_id": clean_ds,
        "ds_name": config["ds_name"],
        "ds_accounts": config["ds_accounts"],
        "currency": config["currency"],
    }


@router.get(
    "/connections/{connection_id}/supermetrics/reports",
    response_model=SupermetricsReportResponse,
)
def get_supermetrics_reports(
    connection_id: str,
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    ds_id: Optional[str] = Query(None),
    campaign_id: Optional[str] = Query(None),
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    """Retrieve persisted Supermetrics marketing reports for this connection."""
    conn = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Integration connection not found.")
    if conn.provider_key != "supermetrics":
        raise HTTPException(status_code=400, detail="Connection is not a Supermetrics integration.")

    query = db.query(SupermetricsMarketingReport).filter_by(
        organization_id=context.organization_id,
        connection_id=connection_id,
    )

    if ds_id:
        query = query.filter(SupermetricsMarketingReport.ds_id == ds_id.strip().upper())
    if start_date:
        query = query.filter(SupermetricsMarketingReport.report_date >= start_date)
    if end_date:
        query = query.filter(SupermetricsMarketingReport.report_date <= end_date)
    if campaign_id:
        query = query.filter(SupermetricsMarketingReport.campaign_id == campaign_id)

    total = query.count()
    all_matching = query.all()

    total_cost = sum((r.cost for r in all_matching), Decimal("0.000000"))
    total_conv_val = sum((r.conversions_value for r in all_matching), Decimal("0.000000"))
    currency_code = (conn.config_json or {}).get("currency") or "USD"

    summary = SupermetricsReportSummary(
        currency=currency_code,
        currency_code=currency_code,
        total_impressions=sum(r.impressions for r in all_matching),
        total_clicks=sum(r.clicks for r in all_matching),
        total_cost=total_cost,
        total_conversions=sum(r.conversions for r in all_matching),
        total_conversions_value=total_conv_val,
    )

    rows = (
        query.order_by(
            SupermetricsMarketingReport.report_date.desc(),
            SupermetricsMarketingReport.campaign_id.asc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    items = [
        SupermetricsReportRowSchema(
            id=r.id,
            organization_id=r.organization_id,
            connection_id=r.connection_id,
            ds_id=r.ds_id,
            ds_account_id=r.ds_account_id,
            report_date=r.report_date,
            campaign_id=r.campaign_id,
            campaign_name=r.campaign_name,
            impressions=r.impressions,
            clicks=r.clicks,
            cost=r.cost,
            conversions=r.conversions,
            conversions_value=r.conversions_value,
            currency=r.currency,
            raw_metrics_json=r.raw_metrics_json,
            sync_job_id=r.sync_job_id,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]

    return SupermetricsReportResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        summary=summary,
    )


@router.post(
    "/connections/{connection_id}/supermetrics/disconnect",
    response_model=SupermetricsDisconnectResponse,
)
def disconnect_supermetrics(
    connection_id: str,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Disconnect Supermetrics integration and purge stored API credentials.

    Requires Owner or Admin role.
    """
    now = datetime.now(timezone.utc)
    conn = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Integration connection not found.")
    if conn.provider_key != "supermetrics":
        raise HTTPException(status_code=400, detail="Connection is not a Supermetrics integration.")

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
            "provider_key": "supermetrics",
            "action": "disconnected",
        },
        created_at=now,
    )
    db.add(audit_entry)
    db.commit()

    return SupermetricsDisconnectResponse(status="disconnected", disconnected=True)

