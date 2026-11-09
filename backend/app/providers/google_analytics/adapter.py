from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from app.core.security import decrypt_secret
from app.models.entities import GA4ReportRow, IntegrationConnection, SyncJob
from app.providers.google_analytics.oauth import GoogleOAuthError, refresh_access_token
from app.providers.google_analytics.reports import fetch_ga4_report
from app.workers.local_worker import PermanentSyncError, RetryableSyncError, SyncWorkerError


class GoogleAnalyticsAdapter:
    """Production provider adapter for Google Analytics 4 reporting ingestion."""

    def __init__(self, db: Session, client: httpx.Client | None = None):
        self.db = db
        self.client = client

    def execute(self, connection: IntegrationConnection, job: SyncJob) -> dict[str, Any]:
        config = connection.config_json or {}
        encrypted_rt = config.get("refresh_token_encrypted")
        if not encrypted_rt:
            raise PermanentSyncError("Connection is not authorized or missing encrypted refresh token.")

        try:
            refresh_token = decrypt_secret(encrypted_rt)
            if not refresh_token:
                raise PermanentSyncError("Decrypted refresh token is empty.")
        except Exception as exc:
            raise PermanentSyncError(f"Failed to decrypt stored credentials: {exc}") from exc

        property_id = config.get("property_id")
        if not property_id:
            raise PermanentSyncError("Connection is missing a selected GA4 property. Property configuration required.")

        start_date = job.metadata_json.get("start_date", "30daysAgo") if job.metadata_json else "30daysAgo"
        end_date = job.metadata_json.get("end_date", "yesterday") if job.metadata_json else "yesterday"

        # 1. Obtain fresh access token via OAuth refresh
        try:
            access_token = refresh_access_token(refresh_token, client=self.client)
        except GoogleOAuthError as exc:
            err_str = str(exc)
            if "Permanent" in err_str or "invalid_grant" in err_str:
                raise PermanentSyncError(f"Permanent Google OAuth authorization error: {err_str}") from exc
            raise RetryableSyncError(f"Transient Google token refresh error: {err_str}") from exc

        # 2. Query Data API report
        rows = fetch_ga4_report(
            access_token=access_token,
            property_id=property_id,
            start_date=start_date,
            end_date=end_date,
            client=self.client,
        )

        # Verify job is still running and lease has not expired before persisting
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

        # 3. Upsert normalized reporting rows into database
        now = datetime.now(timezone.utc)
        total_sessions = 0
        total_active_users = 0
        total_event_count = 0

        for r in rows:
            total_sessions += r.sessions
            total_active_users += r.active_users
            total_event_count += r.event_count

            # Look up existing record to perform idempotent safe upsert
            existing_row = (
                self.db.query(GA4ReportRow)
                .filter_by(
                    organization_id=connection.organization_id,
                    connection_id=connection.id,
                    property_id=property_id,
                    report_date=r.report_date,
                    channel_group=r.channel_group,
                )
                .first()
            )

            if existing_row:
                existing_row.sessions = r.sessions
                existing_row.active_users = r.active_users
                existing_row.event_count = r.event_count
                existing_row.sync_job_id = job.id
                existing_row.updated_at = now
            else:
                new_row = GA4ReportRow(
                    id=uuid4(),
                    organization_id=connection.organization_id,
                    connection_id=connection.id,
                    property_id=property_id,
                    report_date=r.report_date,
                    channel_group=r.channel_group,
                    sessions=r.sessions,
                    active_users=r.active_users,
                    event_count=r.event_count,
                    sync_job_id=job.id,
                    created_at=now,
                    updated_at=now,
                )
                self.db.add(new_row)

        self.db.flush()

        return {
            "property_id": property_id,
            "start_date": start_date,
            "end_date": end_date,
            "rows_synced": len(rows),
            "total_sessions": total_sessions,
            "total_active_users": total_active_users,
            "total_event_count": total_event_count,
        }

