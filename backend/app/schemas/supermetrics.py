from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
from pydantic import BaseModel, Field


class SupermetricsConfigRequest(BaseModel):
    api_key: str = Field(..., min_length=1, description="Supermetrics API Key")
    display_name: str | None = Field(None, max_length=160)


class SupermetricsConfigResponse(BaseModel):
    status: str
    connection_id: str
    has_secret: bool = True
    message: str = "Supermetrics API credentials validated and stored encrypted."


class SupermetricsDataSource(BaseModel):
    ds_id: str
    name: str
    category: str | None = None
    description: str | None = None


class SupermetricsDataSourceListResponse(BaseModel):
    sources: list[SupermetricsDataSource]


class SupermetricsLogin(BaseModel):
    login_id: str
    ds_id: str
    username: str | None = None
    display_name: str | None = None


class SupermetricsLoginListResponse(BaseModel):
    logins: list[SupermetricsLogin]


class SupermetricsAccount(BaseModel):
    account_id: str
    account_name: str
    ds_id: str
    login_id: str | None = None
    currency: str | None = None


class SupermetricsAccountListResponse(BaseModel):
    accounts: list[SupermetricsAccount]


class SupermetricsSourceSelectRequest(BaseModel):
    ds_id: str = Field(..., min_length=1, max_length=64)
    ds_name: str | None = Field(None, max_length=255)
    ds_accounts: str = Field("list.all_accounts", max_length=255)
    ds_account_name: str | None = Field(None, max_length=255)
    currency: str | None = Field(None, max_length=10)


class SupermetricsReportRowSchema(BaseModel):
    id: UUID
    organization_id: UUID
    connection_id: str
    ds_id: str
    ds_account_id: str
    report_date: str
    campaign_id: str
    campaign_name: str | None
    impressions: int
    clicks: int
    cost: Decimal
    conversions: float
    conversions_value: Decimal
    currency: str
    raw_metrics_json: dict[str, Any] | None = None
    sync_job_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class SupermetricsReportSummary(BaseModel):
    currency: str | None = None
    currency_code: str | None = None
    total_impressions: int = 0
    total_clicks: int = 0
    total_cost: Decimal = Decimal("0.000000")
    total_conversions: float = 0.0
    total_conversions_value: Decimal = Decimal("0.000000")


class SupermetricsReportResponse(BaseModel):
    items: list[SupermetricsReportRowSchema]
    total: int
    limit: int
    offset: int
    summary: SupermetricsReportSummary


class SupermetricsDisconnectResponse(BaseModel):
    status: str
    disconnected: bool

