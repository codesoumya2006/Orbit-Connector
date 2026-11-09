"""Add sync_jobs table for job lifecycle and worker foundation.

Revision ID: 0003_sync_jobs
Revises: 0002_firebase_tenants_and_audit
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_sync_jobs"
down_revision: Union[str, None] = "0002_firebase_tenants_and_audit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sync_jobs",
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
        sa.Column(
            "requested_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("job_type", sa.String(64), nullable=False, server_default="incremental_sync"),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
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
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("result", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("correlation_id", sa.String(128), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retrying', 'succeeded', 'failed', 'cancelled')",
            name="ck_sync_jobs_status",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_sync_jobs_org_idempotency_key",
        ),
    )
    op.create_index("ix_sync_jobs_organization_id", "sync_jobs", ["organization_id"])
    op.create_index("ix_sync_jobs_connection_id", "sync_jobs", ["connection_id"])
    op.create_index("ix_sync_jobs_requested_by_user_id", "sync_jobs", ["requested_by_user_id"])
    op.create_index("ix_sync_jobs_status", "sync_jobs", ["status"])
    op.create_index("ix_sync_jobs_idempotency_key", "sync_jobs", ["idempotency_key"])
    op.create_index("ix_sync_jobs_next_retry_at", "sync_jobs", ["next_retry_at"])
    op.create_index("ix_sync_jobs_lease_expires_at", "sync_jobs", ["lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_sync_jobs_lease_expires_at", table_name="sync_jobs")
    op.drop_index("ix_sync_jobs_next_retry_at", table_name="sync_jobs")
    op.drop_index("ix_sync_jobs_idempotency_key", table_name="sync_jobs")
    op.drop_index("ix_sync_jobs_status", table_name="sync_jobs")
    op.drop_index("ix_sync_jobs_requested_by_user_id", table_name="sync_jobs")
    op.drop_index("ix_sync_jobs_connection_id", table_name="sync_jobs")
    op.drop_index("ix_sync_jobs_organization_id", table_name="sync_jobs")
    op.drop_table("sync_jobs")

