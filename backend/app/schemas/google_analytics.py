from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field


class OAuthAuthorizeResponse(BaseModel):
    authorization_url: str
    state: str
    connection_id: str


class PropertyItem(BaseModel):
    property_id: str
    display_name: str
    account_id: str
    account_name: str


class PropertySelectRequest(BaseModel):
    property_id: str = Field(..., min_length=1, max_length=64)
    display_name: str | None = Field(None, max_length=160)
    account_id: str | None = Field(None, max_length=64)
    account_name: str | None = Field(None, max_length=160)


class GA4ReportRowSchema(BaseModel):
    id: UUID
    property_id: str
    report_date: str
    channel_group: str
    sessions: int
    active_users: int
    event_count: int
    sync_job_id: UUID | None
    created_at: datetime
    updated_at: datetime


class GA4ReportResponse(BaseModel):
    items: list[GA4ReportRowSchema]
    total: int
    limit: int
    offset: int

