from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decrypt_secret
from app.models.entities import GoogleAdsCampaignReport, IntegrationConnection, SyncJob
from app.providers.google_ads.oauth import GoogleAdsOAuthError, refresh_access_token
from app.providers.google_ads.reports import (
    cost_micros_to_decimal,
    fetch_google_ads_campaign_report,
)
from app.workers.local_worker import PermanentSyncError, RetryableSyncError, SyncWorkerError


class GoogleAdsAdapter:
    """Production provider adapter for Google Ads campaign reporting ingestion."""

    def __init__(self, db: Session, client: httpx.Client | None = None):
        self.db = db
        self.client = client

    def execute(self, connection: IntegrationConnection, job: SyncJob) -> dict[str, Any]:
        config = connection.config_json or {}
        encrypted_rt = config.get("refresh_token_encrypted")
        if not encrypted_rt:
            raise PermanentSyncError(
                "Connection is not authorized or missing encrypted refresh token."
            )

        try:
            refresh_token = decrypt_secret(encrypted_rt)
            if not refresh_token:
                raise PermanentSyncError("Decrypted refresh token is empty.")
        except Exception as exc:
            raise PermanentSyncError(f"Failed to decrypt stored credentials: {exc}") from exc

        customer_id = config.get("customer_id")
        if not customer_id:
            raise PermanentSyncError(
                "Connection is missing a selected Google Ads customer. Customer selection required."
            )

        developer_token = settings.google_ads_developer_token
        if not developer_token:
            raise PermanentSyncError(
                "Google Ads Developer Token is not configured in backend environment."
            )

        login_customer_id = config.get("login_customer_id")
        currency_code = config.get("currency_code") or "USD"
        job_meta = job.metadata_json or {}
        start_date = job_meta.get("start_date", "30daysAgo")
        end_date = job_meta.get("end_date", "yesterday")
        campaign_ids = job_meta.get("campaign_ids")

        # 1. Obtain fresh access token via OAuth refresh
        try:
            access_token = refresh_access_token(refresh_token, client=self.client)
        except GoogleAdsOAuthError as exc:
            err_str = str(exc)
            if "Permanent" in err_str or "invalid_grant" in err_str:
                raise PermanentSyncError(
                    f"Permanent Google Ads OAuth authorization error: {err_str}"
                ) from exc
            raise RetryableSyncError(
                f"Transient Google Ads token refresh error: {err_str}"
            ) from exc

        # 2. Query Google Ads Campaign Report (v26 with full pagination)
        rows = fetch_google_ads_campaign_report(
            access_token=access_token,
            developer_token=developer_token,
            customer_id=customer_id,
            start_date=start_date,
            end_date=end_date,
            login_customer_id=login_customer_id,
            campaign_ids=campaign_ids,
            client=self.client,
        )

        # 3. Verify job is still running and lease has not expired before persisting
        current_job = self.db.query(SyncJob).filter_by(id=job.id).first()
        if current_job:
            if current_job.status != "running":
                raise PermanentSyncError(
                    f"Job '{job.id}' status changed to '{current_job.status}' during fetch; aborting persistence."
                )
            if current_job.lease_expires_at:
                lease_exp = current_job.lease_expires_at
                if lease_exp.tzinfo is None:
                    lease_exp = lease_exp.replace(tzinfo=timezone.utc)
                if lease_exp < datetime.now(timezone.utc):
                    raise SyncWorkerError(
                        f"Lease for job '{job.id}' expired during fetch; aborting persistence."
                    )

        # 4. Upsert normalized reporting rows into database
        now = datetime.now(timezone.utc)
        total_impressions = 0
        total_clicks = 0
        total_cost_micros = 0
        total_conversions_dec = Decimal("0.0")
        total_conversions_value_dec = Decimal("0.0")

        for r in rows:
            total_impressions += r.impressions
            total_clicks += r.clicks
            total_cost_micros += r.cost_micros
            total_conversions_dec += Decimal(str(r.conversions))
            total_conversions_value_dec += Decimal(str(r.conversions_value))

            existing_row = (
                self.db.query(GoogleAdsCampaignReport)
                .filter_by(
                    organization_id=connection.organization_id,
                    connection_id=connection.id,
                    customer_id=customer_id,
                    report_date=r.report_date,
                    campaign_id=r.campaign_id,
                )
                .first()
            )

            if existing_row:
                existing_row.campaign_name = r.campaign_name
                existing_row.campaign_status = r.campaign_status
                existing_row.impressions = r.impressions
                existing_row.clicks = r.clicks
                existing_row.cost_micros = r.cost_micros
                existing_row.cost = r.cost
                existing_row.conversions = r.conversions
                existing_row.conversions_value = r.conversions_value
                existing_row.sync_job_id = job.id
                existing_row.updated_at = now
            else:
                new_row = GoogleAdsCampaignReport(
                    id=uuid4(),
                    organization_id=connection.organization_id,
                    connection_id=connection.id,
                    customer_id=customer_id,
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
                    sync_job_id=job.id,
                    created_at=now,
                    updated_at=now,
                )
                self.db.add(new_row)

        self.db.flush()

        # Compute exact financial totals using Decimal
        total_cost_dec = cost_micros_to_decimal(total_cost_micros)

        return {
            "customer_id": customer_id,
            "currency_code": currency_code,
            "start_date": start_date,
            "end_date": end_date,
            "rows_synced": len(rows),
            "total_impressions": total_impressions,
            "total_clicks": total_clicks,
            "total_cost_micros": total_cost_micros,
            "total_cost": str(total_cost_dec),
            "total_conversions": float(total_conversions_dec),
            "total_conversions_value": float(total_conversions_value_dec),
        }
