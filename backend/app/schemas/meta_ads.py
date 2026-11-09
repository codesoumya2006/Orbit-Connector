from datetime import datetime
from decimal import Decimal
from uuid import UUID
from pydantic import BaseModel, Field


class MetaAdsOAuthAuthorizeResponse(BaseModel):
    authorization_url: str
    state: str
    connection_id: str


class MetaAdsAccount(BaseModel):
    ad_account_id: str
    name: str
    account_status: int = 1
    currency: str | None = None
    timezone_name: str | None = None
    business_name: str | None = None
    is_prepay: bool = False


class MetaAdsAccountListResponse(BaseModel):
    accounts: list[MetaAdsAccount]


class MetaAdsAccountSelectRequest(BaseModel):
    ad_account_id: str = Field(..., min_length=1, max_length=64)
    name: str | None = Field(None, max_length=255)


class MetaAdsCampaignReportRowSchema(BaseModel):
    id: UUID
    organization_id: UUID
    connection_id: str
    ad_account_id: str
    report_date: str
    campaign_id: str
    campaign_name: str
    campaign_status: str
    impressions: int
    reach: int
    clicks: int
    spend: Decimal
    ctr: float
    cpc: Decimal
    cpm: Decimal
    frequency: float
    conversions: float
    conversions_value: Decimal
    sync_job_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class MetaAdsReportSummary(BaseModel):
    currency: str | None = None
    currency_code: str | None = None
    total_impressions: int = 0
    total_reach: int = 0
    total_clicks: int = 0
    total_spend: Decimal = Decimal("0")
    total_conversions: float = 0.0
    total_conversions_value: Decimal = Decimal("0")


class MetaAdsReportResponse(BaseModel):
    items: list[MetaAdsCampaignReportRowSchema]
    total: int
    limit: int
    offset: int
    summary: MetaAdsReportSummary


class MetaAdsDisconnectResponse(BaseModel):
    status: str
    disconnected: bool

