from datetime import datetime, timezone
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import encrypt_secret
from app.core.context import OrganizationContext, require_permission
from app.core.firebase_auth import AuthenticatedPrincipal, get_current_principal
from app.models.entities import AuditEvent, CustomConnector, IntegrationConnection
from app.schemas.integrations import ConnectionCreate, CustomConnectorCreate, CustomConnectorTest
from app.connectors.registry import list_providers, get_provider
from app.connectors.custom_rest import test_rest_endpoint, ConnectorSafetyError

router = APIRouter()
@router.get("/health")
def health():
    return {"status": "ok", "service": "connector-hub-api"}

@router.get("/providers")
def providers(db: Session = Depends(get_db)):
    rows = list_providers()
    for item in rows:
        item["connection_count"] = (
            db.query(IntegrationConnection).filter_by(provider_key=item["key"]).count()
        )
    return rows


@router.get("/connections")
def connections(
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    return [
        {
            "id": c.id,
            "provider_key": c.provider_key,
            "display_name": c.display_name,
            "status": c.status,
            "connection_type": c.connection_type,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "has_secret": bool("refresh_token_encrypted" in (c.config_json or {}) or "access_token_encrypted" in (c.config_json or {}) or "api_key_encrypted" in (c.config_json or {})),
            "property_id": (c.config_json or {}).get("property_id"),
            "property_name": (c.config_json or {}).get("property_name"),
            "account_name": (c.config_json or {}).get("account_name"),
            "customer_id": (c.config_json or {}).get("customer_id"),
            "customer_name": (c.config_json or {}).get("customer_name"),
            "login_customer_id": (c.config_json or {}).get("login_customer_id"),
            "ad_account_id": (c.config_json or {}).get("ad_account_id"),
            "ad_account_name": (c.config_json or {}).get("ad_account_name"),
            "currency": (c.config_json or {}).get("currency"),
            "ds_id": (c.config_json or {}).get("ds_id"),
            "ds_name": (c.config_json or {}).get("ds_name"),
            "ds_accounts": (c.config_json or {}).get("ds_accounts"),
            "selected_model": (c.config_json or {}).get("selected_model"),
        }
        for c in db.query(IntegrationConnection)
        .filter_by(organization_id=context.organization_id)
        .order_by(IntegrationConnection.created_at.desc())
        .all()
    ]


@router.get("/connections/{connection_id}")
def get_connection(
    connection_id: str,
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    c = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not c:
        raise HTTPException(status_code=404, detail="Integration connection not found.")
    return {
        "id": c.id,
        "provider_key": c.provider_key,
        "display_name": c.display_name,
        "status": c.status,
        "connection_type": c.connection_type,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "has_secret": bool("refresh_token_encrypted" in (c.config_json or {}) or "access_token_encrypted" in (c.config_json or {}) or "api_key_encrypted" in (c.config_json or {})),
        "property_id": (c.config_json or {}).get("property_id"),
        "property_name": (c.config_json or {}).get("property_name"),
        "account_name": (c.config_json or {}).get("account_name"),
        "customer_id": (c.config_json or {}).get("customer_id"),
        "customer_name": (c.config_json or {}).get("customer_name"),
        "login_customer_id": (c.config_json or {}).get("login_customer_id"),
        "ad_account_id": (c.config_json or {}).get("ad_account_id"),
        "ad_account_name": (c.config_json or {}).get("ad_account_name"),
        "currency": (c.config_json or {}).get("currency"),
        "ds_id": (c.config_json or {}).get("ds_id"),
        "ds_name": (c.config_json or {}).get("ds_name"),
        "ds_accounts": (c.config_json or {}).get("ds_accounts"),
        "selected_model": (c.config_json or {}).get("selected_model"),
    }


@router.post("/connections", status_code=status.HTTP_201_CREATED)
def create_connection(
    payload: ConnectionCreate,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    if payload.organization_id:
        try:
            body_org_uuid = UUID(str(payload.organization_id))
            if body_org_uuid != context.organization_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Organization ID in request body does not match target organization context.",
                )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid organization ID format in request body.",
            )

    provider = get_provider(payload.provider_key)
    if not provider:
        raise HTTPException(status_code=404, detail="Unknown provider.")

    now = datetime.now(timezone.utc)
    row = IntegrationConnection(
        id=str(uuid4()),
        organization_id=context.organization_id,
        provider_key=payload.provider_key,
        display_name=payload.display_name,
        status="needs_authorization",
        connection_type=payload.connection_type,
        config_json={},
        created_at=now,
    )
    audit = AuditEvent(
        actor_user_id=context.user_id,
        organization_id=context.organization_id,
        event_type="connection.created",
        target_type="connection",
        target_id=row.id,
        created_at=now,
        metadata_json={
            "provider_key": payload.provider_key,
            "display_name": payload.display_name,
            "connection_type": payload.connection_type,
        },
    )
    try:
        db.add(row)
        db.flush()
        db.add(audit)
        db.commit()
        db.refresh(row)
        return {
            "id": row.id,
            "status": row.status,
            "message": "Connection record created. Complete provider authorization to activate it.",
        }
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create connection.",
        ) from exc


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(
    connection_id: str,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    row = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Connection not found.")

    now = datetime.now(timezone.utc)
    audit = AuditEvent(
        actor_user_id=context.user_id,
        organization_id=context.organization_id,
        event_type="connection.deleted",
        target_type="connection",
        target_id=row.id,
        created_at=now,
        metadata_json={
            "provider_key": row.provider_key,
            "display_name": row.display_name,
        },
    )
    try:
        db.delete(row)
        db.add(audit)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete connection.",
        ) from exc


@router.get("/custom-connectors")
def custom_connectors(
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(CustomConnector)
        .filter_by(organization_id=context.organization_id)
        .order_by(CustomConnector.created_at.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "name": r.name,
            "base_url": r.base_url,
            "endpoint_path": r.endpoint_path,
            "auth_type": r.auth_type,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "has_secret": bool(r.secret_encrypted),
        }
        for r in rows
    ]


@router.post("/custom-connectors/test")
async def test_custom_connector(
    payload: CustomConnectorTest,
    context: OrganizationContext = Depends(require_permission("connector:write")),
):
    try:
        return await test_rest_endpoint(
            str(payload.base_url),
            payload.endpoint_path,
            payload.auth_type,
            payload.secret,
            payload.secret_label,
            payload.headers,
        )
    except (ConnectorSafetyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Connection test failed: {type(exc).__name__}"
        )


@router.post("/custom-connectors", status_code=status.HTTP_201_CREATED)
def create_custom_connector(
    payload: CustomConnectorCreate,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    if payload.organization_id:
        try:
            body_org_uuid = UUID(str(payload.organization_id))
            if body_org_uuid != context.organization_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Organization ID in request body does not match target organization context.",
                )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid organization ID format in request body.",
            )

    if payload.auth_type != "none" and not payload.secret:
        raise HTTPException(
            status_code=422,
            detail="Credentials are required for the selected authentication type.",
        )

    now = datetime.now(timezone.utc)
    row = CustomConnector(
        id=str(uuid4()),
        organization_id=context.organization_id,
        name=payload.name,
        base_url=str(payload.base_url),
        auth_type=payload.auth_type,
        secret_encrypted=encrypt_secret(payload.secret),
        secret_label=payload.secret_label,
        headers_json=payload.headers,
        endpoint_path=payload.endpoint_path,
        created_at=now,
    )
    audit = AuditEvent(
        actor_user_id=context.user_id,
        organization_id=context.organization_id,
        event_type="custom_connector.created",
        target_type="custom_connector",
        target_id=row.id,
        created_at=now,
        metadata_json={
            "name": payload.name,
            "base_url": str(payload.base_url),
            "auth_type": payload.auth_type,
            "endpoint_path": payload.endpoint_path,
            "secret_label": payload.secret_label,
        },
    )
    try:
        db.add(row)
        db.flush()
        db.add(audit)
        db.commit()
        db.refresh(row)
        return {
            "id": row.id,
            "name": row.name,
            "status": "saved",
            "message": "Custom connector saved. Configure a dataset mapping and sync schedule next.",
        }
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create custom connector.",
        ) from exc


@router.delete("/custom-connectors/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_custom_connector(
    connector_id: str,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    row = (
        db.query(CustomConnector)
        .filter_by(id=connector_id, organization_id=context.organization_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Custom connector not found.")

    now = datetime.now(timezone.utc)
    audit = AuditEvent(
        actor_user_id=context.user_id,
        organization_id=context.organization_id,
        event_type="custom_connector.deleted",
        target_type="custom_connector",
        target_id=row.id,
        created_at=now,
        metadata_json={
            "name": row.name,
            "base_url": row.base_url,
            "auth_type": row.auth_type,
        },
    )
    try:
        db.delete(row)
        db.add(audit)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete custom connector.",
        ) from exc
