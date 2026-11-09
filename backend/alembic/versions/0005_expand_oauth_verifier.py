"""Expand oauth_states.code_verifier to String(512) for PKCE encryption.

Revision ID: 0005_expand_oauth_verifier
Revises: 0004_ga4_integration
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_expand_oauth_verifier"
down_revision: Union[str, None] = "0004_ga4_integration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("oauth_states") as batch_op:
        batch_op.alter_column(
            "code_verifier",
            existing_type=sa.String(128),
            type_=sa.String(512),
            existing_nullable=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "oauth_states" in inspector.get_table_names():
        long_rows = bind.execute(
            sa.text("SELECT COUNT(*) FROM oauth_states WHERE LENGTH(code_verifier) > 128")
        ).scalar()
        if long_rows and long_rows > 0:
            raise RuntimeError(
                f"Cannot downgrade migration 0005: {long_rows} oauth_states rows contain "
                "encrypted code_verifier values exceeding 128 characters. Truncating would destroy "
                "cryptographic tokens. Clean up or expire old rows before downgrading."
            )

    with op.batch_alter_table("oauth_states") as batch_op:
        batch_op.alter_column(
            "code_verifier",
            existing_type=sa.String(512),
            type_=sa.String(128),
            existing_nullable=False,
        )

