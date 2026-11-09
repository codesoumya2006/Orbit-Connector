"""Supermetrics provider sync adapter for local background worker execution."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from app.core.security import decrypt_secret
from app.models.entities import IntegrationConnection, SupermetricsMarketingReport, SyncJob
from app.providers.supermetrics.client import query_supermetrics_data
from app.workers.local_worker import PermanentSyncError, SyncWorkerError


class SupermetricsAdapter:
    """Production provider adapter for Supermetrics multi-source marketing data ingestion."""

    def __init__(self, db: Session, client: httpx.Client | None = None):
        self.db = db
        self.client = client

    def execute(self, connection: IntegrationConnection, job: SyncJob) -> dict[str, Any]:
        config = connection.config_json or {}
        encrypted_api_key = config.get("api_key_encrypted")
        if not encrypted_api_key:
            raise PermanentSyncError(
                "Connection is not configured with an encrypted Supermetrics API key."
            )

        try:
            api_key = decrypt_secret(encrypted_api_key)
            if not api_key:
                raise PermanentSyncError("Decrypted Supermetrics API key is empty.")
        except Exception as exc:
            raise PermanentSyncError(f"Failed to decrypt Supermetrics credentials: {exc}") from exc

        ds_id = config.get("ds_id")
        if not ds_id:
            raise PermanentSyncError(
                "Connection is missing a configured Supermetrics data source (ds_id)."
            )

        ds_accounts = config.get("ds_accounts") or "list.all_accounts"
        currency = config.get("currency") or "USD"

        job_meta = job.metadata_json or {}
        start_date = job_meta.get("start_date", "30daysAgo")
        end_date = job_meta.get("end_date", "yesterday")
        fields = job_meta.get("fields")

        # 1. Fetch marketing data via Supermetrics client
        rows = query_supermetrics_data(
            api_key=api_key,
            ds_id=ds_id,
            ds_accounts=ds_accounts,
            start_date=start_date,
            end_date=end_date,
            fields=fields,
            client=self.client,
        )

        # 2. Verify worker lease has not expired and job is not cancelled before persisting
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

        # 3. Idempotently upsert rows into supermetrics_marketing_reports table
        now = datetime.now(timezone.utc)
        total_impressions = 0
        total_clicks = 0
        total_cost = Decimal("0.000000")
        total_conversions = 0.0
        total_conversions_value = Decimal("0.000000")

        for r in rows:
            total_impressions += r.impressions
            total_clicks += r.clicks
            total_cost += r.cost
            total_conversions += r.conversions
            total_conversions_value += r.conversions_value

            existing = (
                self.db.query(SupermetricsMarketingReport)
                .filter_by(
                    organization_id=connection.organization_id,
                    connection_id=connection.id,
                    ds_id=r.ds_id,
                    ds_account_id=r.ds_account_id,
                    report_date=r.report_date,
                    campaign_id=r.campaign_id,
                )
                .first()
            )

            row_curr = r.currency or currency

            if existing:
                existing.campaign_name = r.campaign_name
                existing.impressions = r.impressions
                existing.clicks = r.clicks
                existing.cost = r.cost
                existing.conversions = r.conversions
                existing.conversions_value = r.conversions_value
                existing.currency = row_curr
                existing.raw_metrics_json = r.raw_metrics_json
                existing.sync_job_id = job.id
                existing.updated_at = now
            else:
                new_row = SupermetricsMarketingReport(
                    id=uuid4(),
                    organization_id=connection.organization_id,
                    connection_id=connection.id,
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
                    currency=row_curr,
                    raw_metrics_json=r.raw_metrics_json,
                    sync_job_id=job.id,
                    created_at=now,
                    updated_at=now,
                )
                self.db.add(new_row)

        self.db.flush()

        return {
            "ds_id": ds_id,
            "ds_accounts": ds_accounts,
            "currency": currency,
            "start_date": start_date,
            "end_date": end_date,
            "rows_synced": len(rows),
            "total_impressions": total_impressions,
            "total_clicks": total_clicks,
            "total_cost": str(total_cost),
            "total_conversions": total_conversions,
            "total_conversions_value": str(total_conversions_value),
        }

