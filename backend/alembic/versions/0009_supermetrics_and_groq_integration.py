"""Add supermetrics_marketing_reports table for Supermetrics connector.

Revision ID: 0009_supermetrics_and_groq_integration
Revises: 0008_meta_ads_integration
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009_supermetrics_and_groq_integration"
down_revision: Union[str, None] = "0008_meta_ads_integration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)"))

    op.create_table(
        "supermetrics_marketing_reports",
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
        sa.Column("ds_id", sa.String(64), nullable=False),
        sa.Column("ds_account_id", sa.String(128), nullable=False),
        sa.Column("report_date", sa.String(10), nullable=False),
        sa.Column("campaign_id", sa.String(128), nullable=False, server_default=sa.text("'all'")),
        sa.Column("campaign_name", sa.String(255), nullable=True),
        sa.Column("impressions", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("clicks", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "cost",
            sa.Numeric(precision=20, scale=6),
            nullable=False,
            server_default=sa.text("0.000000"),
        ),
        sa.Column("conversions", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column(
            "conversions_value",
            sa.Numeric(precision=20, scale=6),
            nullable=False,
            server_default=sa.text("0.000000"),
        ),
        sa.Column("currency", sa.String(10), nullable=False, server_default=sa.text("'USD'")),
        sa.Column("raw_metrics_json", sa.JSON(), nullable=True),
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
            "ds_id",
            "ds_account_id",
            "report_date",
            "campaign_id",
            name="uq_supermetrics_report_row",
        ),
    )

    op.create_index(
        "ix_supermetrics_org_conn_date",
        "supermetrics_marketing_reports",
        ["organization_id", "connection_id", "report_date"],
    )
    op.create_index(
        "ix_supermetrics_ds_id",
        "supermetrics_marketing_reports",
        ["ds_id"],
    )
    op.create_index(
        "ix_supermetrics_ds_account_id",
        "supermetrics_marketing_reports",
        ["ds_account_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_supermetrics_ds_account_id", table_name="supermetrics_marketing_reports")
    op.drop_index("ix_supermetrics_ds_id", table_name="supermetrics_marketing_reports")
    op.drop_index("ix_supermetrics_org_conn_date", table_name="supermetrics_marketing_reports")
    op.drop_table("supermetrics_marketing_reports")

