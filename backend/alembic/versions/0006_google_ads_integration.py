"""Add google_ads_campaign_reports table for Google Ads integration.

Revision ID: 0006_google_ads_integration
Revises: 0005_expand_oauth_verifier
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006_google_ads_integration"
down_revision: Union[str, None] = "0005_expand_oauth_verifier"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "google_ads_campaign_reports",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "organization_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.String(36),
            sa.ForeignKey("integration_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("report_date", sa.String(10), nullable=False),
        sa.Column("campaign_id", sa.String(64), nullable=False),
        sa.Column("campaign_name", sa.String(255), nullable=False),
        sa.Column("campaign_status", sa.String(64), nullable=False),
        sa.Column("impressions", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("clicks", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("conversions", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("conversions_value", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column(
            "sync_job_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("sync_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "organization_id",
            "connection_id",
            "customer_id",
            "report_date",
            "campaign_id",
            name="uq_google_ads_report_rows_unique_record",
        ),
    )
    op.create_index(
        "ix_google_ads_campaign_reports_organization_id",
        "google_ads_campaign_reports",
        ["organization_id"],
    )
    op.create_index(
        "ix_google_ads_campaign_reports_connection_id",
        "google_ads_campaign_reports",
        ["connection_id"],
    )
    op.create_index(
        "ix_google_ads_campaign_reports_customer_id",
        "google_ads_campaign_reports",
        ["customer_id"],
    )
    op.create_index(
        "ix_google_ads_campaign_reports_report_date",
        "google_ads_campaign_reports",
        ["report_date"],
    )
    op.create_index(
        "ix_google_ads_campaign_reports_campaign_id",
        "google_ads_campaign_reports",
        ["campaign_id"],
    )
    op.create_index(
        "ix_google_ads_campaign_reports_sync_job_id",
        "google_ads_campaign_reports",
        ["sync_job_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_google_ads_campaign_reports_sync_job_id",
        table_name="google_ads_campaign_reports",
    )
    op.drop_index(
        "ix_google_ads_campaign_reports_campaign_id",
        table_name="google_ads_campaign_reports",
    )
    op.drop_index(
        "ix_google_ads_campaign_reports_report_date",
        table_name="google_ads_campaign_reports",
    )
    op.drop_index(
        "ix_google_ads_campaign_reports_customer_id",
        table_name="google_ads_campaign_reports",
    )
    op.drop_index(
        "ix_google_ads_campaign_reports_connection_id",
        table_name="google_ads_campaign_reports",
    )
    op.drop_index(
        "ix_google_ads_campaign_reports_organization_id",
        table_name="google_ads_campaign_reports",
    )
    op.drop_table("google_ads_campaign_reports")

