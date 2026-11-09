"""Add oauth_states and ga4_report_rows for GA4 integration.

Revision ID: 0004_ga4_integration
Revises: 0003_sync_jobs
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_ga4_integration"
down_revision: Union[str, None] = "0003_sync_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. OAuth States table for secure, single-use, expiring state binding
    op.create_table(
        "oauth_states",
        sa.Column("state_token", sa.String(128), primary_key=True, nullable=False),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column(
            "organization_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.String(36),
            sa.ForeignKey("integration_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_oauth_states_organization_id", "oauth_states", ["organization_id"])
    op.create_index("ix_oauth_states_expires_at", "oauth_states", ["expires_at"])

    # 2. GA4 Report Rows table for reporting-data persistence
    op.create_table(
        "ga4_report_rows",
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
        sa.Column("property_id", sa.String(64), nullable=False),
        sa.Column("report_date", sa.String(10), nullable=False),
        sa.Column("channel_group", sa.String(128), nullable=False),
        sa.Column("sessions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_users", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
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
            "property_id",
            "report_date",
            "channel_group",
            name="uq_ga4_report_rows_unique_record",
        ),
    )
    op.create_index(
        "ix_ga4_report_rows_org_conn",
        "ga4_report_rows",
        ["organization_id", "connection_id"],
    )
    op.create_index(
        "ix_ga4_report_rows_property_date",
        "ga4_report_rows",
        ["property_id", "report_date"],
    )
    op.create_index("ix_ga4_report_rows_sync_job_id", "ga4_report_rows", ["sync_job_id"])


def downgrade() -> None:
    op.drop_index("ix_ga4_report_rows_sync_job_id", table_name="ga4_report_rows")
    op.drop_index("ix_ga4_report_rows_property_date", table_name="ga4_report_rows")
    op.drop_index("ix_ga4_report_rows_org_conn", table_name="ga4_report_rows")
    op.drop_table("ga4_report_rows")

    op.drop_index("ix_oauth_states_expires_at", table_name="oauth_states")
    op.drop_index("ix_oauth_states_organization_id", table_name="oauth_states")
    op.drop_table("oauth_states")

