from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.context import OrganizationContext, require_permission
from app.core.database import get_db
from app.core.security import decrypt_secret, encrypt_secret
from app.models.entities import AuditEvent, IntegrationConnection
from app.providers.groq.client import (
    GroqAuthError,
    GroqModelError,
    GroqRateLimitError,
    GroqRequestError,
    execute_groq_completion,
    list_groq_models,
    validate_groq_api_key,
)
from app.providers.groq.constants import DEFAULT_GROQ_MODEL
from app.schemas.groq import (
    GroqCompletionRequest,
    GroqCompletionResponse,
    GroqConfigRequest,
    GroqConfigResponse,
    GroqDisconnectResponse,
    GroqModel,
    GroqModelListResponse,
    GroqModelSelectRequest,
)

router = APIRouter()


@router.post(
    "/connections/{connection_id}/groq/config",
    response_model=GroqConfigResponse,
)
def configure_groq(
    connection_id: str,
    payload: GroqConfigRequest,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Configure Groq API key and model parameters with secure encryption at rest.

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
    if conn.provider_key != "groq":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connection is not a Groq integration.",
        )

    # 1. Real authenticated validation against Groq API
    try:
        available_models = validate_groq_api_key(api_key=payload.api_key)
    except GroqAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Groq API authentication failed: {exc}",
        )
    except GroqRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Groq rate limit: {exc}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Groq connection test failed: {exc}",
        )

    selected_model = payload.selected_model or DEFAULT_GROQ_MODEL
    # If selected model provided, verify it exists or fallback gracefully
    if payload.selected_model and available_models and payload.selected_model not in available_models:
        selected_model = available_models[0]

    # 2. Encrypt credentials at rest
    config = dict(conn.config_json or {})
    config["api_key_encrypted"] = encrypt_secret(payload.api_key.strip())
    config["selected_model"] = selected_model
    config["temperature"] = payload.temperature if payload.temperature is not None else 0.7
    config["max_tokens"] = payload.max_tokens if payload.max_tokens is not None else 256
    config["authorized_at"] = now.isoformat()

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
            "provider_key": "groq",
            "action": "configure_api_key",
            "selected_model": selected_model,
        },
        created_at=now,
    )
    db.add(audit_entry)
    db.commit()

    return GroqConfigResponse(
        status="connected",
        connection_id=conn.id,
        selected_model=selected_model,
        has_secret=True,
        message="Groq API key verified and securely stored encrypted.",
    )


@router.post("/connections/{connection_id}/groq/test")
def test_groq_connection(
    connection_id: str,
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    """Test Groq API connectivity using stored encrypted credentials."""
    conn = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Integration connection not found.")
    if conn.provider_key != "groq":
        raise HTTPException(status_code=400, detail="Connection is not a Groq integration.")

    config = dict(conn.config_json or {})
    encrypted_key = config.get("api_key_encrypted")
    if not encrypted_key:
        raise HTTPException(status_code=400, detail="Groq connection has no stored credentials.")

    try:
        api_key = decrypt_secret(encrypted_key)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to decrypt stored credentials: {exc}")

    try:
        validate_groq_api_key(api_key=api_key)
        return {
            "status": "ok",
            "valid": True,
            "selected_model": config.get("selected_model", DEFAULT_GROQ_MODEL),
            "message": "Groq API key is valid and connected.",
        }
    except GroqAuthError as exc:
        raise HTTPException(status_code=401, detail=f"Groq authentication rejected: {exc}")
    except GroqRateLimitError as exc:
        raise HTTPException(status_code=429, detail=f"Groq rate limit: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Groq connection test failed: {exc}")


@router.get(
    "/connections/{connection_id}/groq/models",
    response_model=GroqModelListResponse,
)
def get_groq_models(
    connection_id: str,
    context: OrganizationContext = Depends(require_permission("connector:read")),
    db: Session = Depends(get_db),
):
    """Retrieve supported models from Groq API endpoint."""
    conn = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Integration connection not found.")
    if conn.provider_key != "groq":
        raise HTTPException(status_code=400, detail="Connection is not a Groq integration.")

    config = dict(conn.config_json or {})
    encrypted_key = config.get("api_key_encrypted")
    if not encrypted_key:
        raise HTTPException(status_code=400, detail="Connection has no stored Groq credentials.")

    try:
        api_key = decrypt_secret(encrypted_key)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to decrypt credentials: {exc}")

    try:
        raw_models = list_groq_models(api_key=api_key)
        models = [
            GroqModel(
                id=m["id"],
                name=m["name"],
                description=m.get("description"),
                owned_by=m.get("owned_by"),
                context_window=m.get("context_window", 8192),
                active=m.get("active", True),
            )
            for m in raw_models
        ]
        return GroqModelListResponse(models=models)
    except GroqAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch Groq models: {exc}")


@router.post("/connections/{connection_id}/groq/models/select")
def select_groq_model(
    connection_id: str,
    payload: GroqModelSelectRequest,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Select the default active Groq model for this connection."""
    now = datetime.now(timezone.utc)
    conn = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Integration connection not found.")
    if conn.provider_key != "groq":
        raise HTTPException(status_code=400, detail="Connection is not a Groq integration.")

    config = dict(conn.config_json or {})
    config["selected_model"] = payload.model_id.strip()

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
            "provider_key": "groq",
            "action": "select_model",
            "selected_model": payload.model_id.strip(),
        },
        created_at=now,
    )
    db.add(audit_entry)
    db.commit()

    return {
        "status": "connected",
        "selected_model": payload.model_id.strip(),
    }


@router.post(
    "/connections/{connection_id}/groq/test-completion",
    response_model=GroqCompletionResponse,
)
def test_groq_completion(
    connection_id: str,
    payload: GroqCompletionRequest,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Execute a test prompt completion using Groq LPU API.

    Requires Owner or Admin role. Does not log prompt or completion contents to audit logs.
    """
    conn = (
        db.query(IntegrationConnection)
        .filter_by(id=connection_id, organization_id=context.organization_id)
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Integration connection not found.")
    if conn.provider_key != "groq":
        raise HTTPException(status_code=400, detail="Connection is not a Groq integration.")

    config = dict(conn.config_json or {})
    encrypted_key = config.get("api_key_encrypted")
    if not encrypted_key:
        raise HTTPException(status_code=400, detail="Groq connection has no stored credentials.")

    try:
        api_key = decrypt_secret(encrypted_key)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to decrypt credentials: {exc}")

    target_model = payload.model or config.get("selected_model") or DEFAULT_GROQ_MODEL
    temp = payload.temperature if payload.temperature is not None else float(config.get("temperature", 0.7))
    token_limit = payload.max_completion_tokens or payload.max_tokens or int(config.get("max_completion_tokens") or config.get("max_tokens", 256))

    try:
        result = execute_groq_completion(
            api_key=api_key,
            prompt=payload.prompt,
            model=target_model,
            temperature=temp,
            max_completion_tokens=token_limit,
        )
        return GroqCompletionResponse(**result)
    except GroqAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except GroqRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except GroqModelError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Groq completion failed: {exc}")


@router.post(
    "/connections/{connection_id}/groq/disconnect",
    response_model=GroqDisconnectResponse,
)
def disconnect_groq(
    connection_id: str,
    context: OrganizationContext = Depends(require_permission("connector:write")),
    db: Session = Depends(get_db),
):
    """Disconnect Groq integration and purge stored credentials.

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
    if conn.provider_key != "groq":
        raise HTTPException(status_code=400, detail="Connection is not a Groq integration.")

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
            "provider_key": "groq",
            "action": "disconnected",
        },
        created_at=now,
    )
    db.add(audit_entry)
    db.commit()

    return GroqDisconnectResponse(status="disconnected", disconnected=True)

