from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from app.core.security import decrypt_secret
from app.models.entities import IntegrationConnection, MetaAdsCampaignReport, SyncJob
from app.providers.meta_ads.insights import fetch_meta_ads_campaign_insights
from app.workers.local_worker import PermanentSyncError, SyncWorkerError


class MetaAdsAdapter:
    """Production provider adapter for Meta Ads campaign insights ingestion."""

    def __init__(self, db: Session, client: httpx.Client | None = None):
        self.db = db
        self.client = client

    def execute(self, connection: IntegrationConnection, job: SyncJob) -> dict[str, Any]:
        config = connection.config_json or {}
        encrypted_token = config.get("access_token_encrypted")
        if not encrypted_token:
            raise PermanentSyncError(
                "Connection is not authorized or missing encrypted Meta access token."
            )

        try:
            access_token = decrypt_secret(encrypted_token)
            if not access_token:
                raise PermanentSyncError("Decrypted Meta access token is empty.")
        except Exception as exc:
            raise PermanentSyncError(f"Failed to decrypt stored Meta credentials: {exc}") from exc

        ad_account_id = config.get("ad_account_id")
        if not ad_account_id:
            raise PermanentSyncError(
                "Connection is missing a selected Meta ad account. Account selection required."
            )

        currency_code = config.get("currency")
        if not currency_code:
            raise PermanentSyncError(
                "Connection is missing currency metadata. Account selection or re-authorization required."
            )
        job_meta = job.metadata_json or {}
        start_date = job_meta.get("start_date", "30daysAgo")
        end_date = job_meta.get("end_date", "yesterday")
        campaign_ids = job_meta.get("campaign_ids")

        # 1. Fetch Insights from Meta Marketing API v27.0
        rows = fetch_meta_ads_campaign_insights(
            access_token=access_token,
            account_id=ad_account_id,
            start_date=start_date,
            end_date=end_date,
            campaign_ids=campaign_ids,
            client=self.client,
        )

        # 2. Verify job is still running and lease has not expired before persisting
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

        # 3. Idempotently upsert rows into meta_ads_campaign_reports table
        now = datetime.now(timezone.utc)
        total_impressions = 0
        total_reach = 0
        total_clicks = 0
        total_spend = Decimal("0.000000")
        total_conversions = 0.0
        total_conversions_value = Decimal("0.000000")

        for r in rows:
            total_impressions += r.impressions
            total_reach += r.reach
            total_clicks += r.clicks
            total_spend += r.spend
            total_conversions += r.conversions
            total_conversions_value += r.conversions_value

            existing_row = (
                self.db.query(MetaAdsCampaignReport)
                .filter_by(
                    organization_id=connection.organization_id,
                    connection_id=connection.id,
                    ad_account_id=ad_account_id,
                    report_date=r.report_date,
                    campaign_id=r.campaign_id,
                )
                .first()
            )

            if existing_row:
                existing_row.campaign_name = r.campaign_name
                existing_row.campaign_status = r.campaign_status
                existing_row.impressions = r.impressions
                existing_row.reach = r.reach
                existing_row.clicks = r.clicks
                existing_row.spend = r.spend
                existing_row.ctr = r.ctr
                existing_row.cpc = r.cpc
                existing_row.cpm = r.cpm
                existing_row.frequency = r.frequency
                existing_row.conversions = r.conversions
                existing_row.conversions_value = r.conversions_value
                existing_row.sync_job_id = job.id
                existing_row.updated_at = now
            else:
                new_row = MetaAdsCampaignReport(
                    id=uuid4(),
                    organization_id=connection.organization_id,
                    connection_id=connection.id,
                    ad_account_id=ad_account_id,
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
                    sync_job_id=job.id,
                    created_at=now,
                    updated_at=now,
                )
                self.db.add(new_row)

        self.db.flush()

        return {
            "ad_account_id": ad_account_id,
            "currency_code": currency_code,
            "start_date": start_date,
            "end_date": end_date,
            "rows_synced": len(rows),
            "total_impressions": total_impressions,
            "total_reach": total_reach,
            "total_clicks": total_clicks,
            "total_spend": str(total_spend),
            "total_conversions": total_conversions,
            "total_conversions_value": str(total_conversions_value),
        }

