from datetime import datetime
from decimal import Decimal
from uuid import UUID
from pydantic import BaseModel, Field


class GoogleAdsConfigRequest(BaseModel):
    developer_token: str = Field(..., min_length=1, max_length=255)


class GoogleAdsConfigResponse(BaseModel):
    status: str
    connection_id: str
    has_developer_token: bool = True
    message: str


class GoogleAdsOAuthAuthorizeResponse(BaseModel):
    authorization_url: str
    state: str
    connection_id: str


class GoogleAdsCustomer(BaseModel):
    customer_id: str
    descriptive_name: str
    is_manager: bool = False
    currency_code: str | None = None
    time_zone: str | None = None
    login_customer_id: str | None = None
    test_account: bool = False


class GoogleAdsCustomerListResponse(BaseModel):
    customers: list[GoogleAdsCustomer]


class GoogleAdsCustomerSelectRequest(BaseModel):
    customer_id: str = Field(..., min_length=1, max_length=64)
    customer_name: str | None = Field(None, max_length=255)
    login_customer_id: str | None = Field(None, max_length=64)


class GoogleAdsCampaignReportRowSchema(BaseModel):
    id: UUID
    organization_id: UUID
    connection_id: str
    customer_id: str
    report_date: str
    campaign_id: str
    campaign_name: str
    campaign_status: str
    impressions: int
    clicks: int
    cost_micros: int
    cost: Decimal
    conversions: float
    conversions_value: float
    sync_job_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class GoogleAdsReportSummary(BaseModel):
    currency_code: str | None = None
    total_impressions: int = 0
    total_clicks: int = 0
    total_cost_micros: int = 0
    total_cost: Decimal = Decimal("0")
    total_conversions: float = 0.0
    total_conversions_value: float = 0.0


class GoogleAdsReportResponse(BaseModel):
    items: list[GoogleAdsCampaignReportRowSchema]
    total: int
    limit: int
    offset: int
    summary: GoogleAdsReportSummary


class GoogleAdsDisconnectResponse(BaseModel):
    status: str
    disconnected: bool
