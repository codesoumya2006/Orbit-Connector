from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class SyncJobCreate(BaseModel):
    connection_id: str = Field(min_length=1, max_length=36)
    job_type: str = Field(default="incremental_sync", max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=128)
    metadata: dict = Field(default_factory=dict)


class SyncJobResponse(BaseModel):
    id: UUID
    organization_id: UUID
    connection_id: str
    requested_by_user_id: UUID | None = None
    job_type: str
    status: str
    idempotency_key: str | None = None
    attempt_count: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    next_retry_at: datetime | None = None
    metadata: dict = Field(default_factory=dict)
    result: dict = Field(default_factory=dict)
    error: dict = Field(default_factory=dict)
    correlation_id: str | None = None

    model_config = {"from_attributes": True}


class SyncJobListResponse(BaseModel):
    items: list[SyncJobResponse]
    total: int
    limit: int
    offset: int

