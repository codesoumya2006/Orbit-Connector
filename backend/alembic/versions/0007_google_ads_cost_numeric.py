"""Alter google_ads_campaign_reports.cost from Float to Numeric(20, 6).

Revision ID: 0007_google_ads_cost_numeric
Revises: 0006_google_ads_integration
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007_google_ads_cost_numeric"
down_revision: Union[str, None] = "0006_google_ads_integration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("google_ads_campaign_reports") as batch_op:
        batch_op.alter_column(
            "cost",
            existing_type=sa.Float(),
            type_=sa.Numeric(precision=20, scale=6),
            existing_nullable=False,
            existing_server_default=sa.text("0.0"),
            server_default=sa.text("0.000000"),
        )


def downgrade() -> None:
    with op.batch_alter_table("google_ads_campaign_reports") as batch_op:
        batch_op.alter_column(
            "cost",
            existing_type=sa.Numeric(precision=20, scale=6),
            type_=sa.Float(),
            existing_nullable=False,
            existing_server_default=sa.text("0.000000"),
            server_default=sa.text("0.0"),
        )

