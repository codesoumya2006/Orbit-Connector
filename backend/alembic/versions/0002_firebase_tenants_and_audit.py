"""Migrate organizations to UUIDs and add Firebase tenant/audit tables.

Revision ID: 0002_firebase_tenants_and_audit
Revises: 0001_existing_schema_baseline
"""

from typing import Sequence, Union
from uuid import UUID, uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "0002_firebase_tenants_and_audit"
down_revision: Union[str, None] = "0001_existing_schema_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CONNECTOR_TABLES = ("integration_connections", "custom_connectors")
_AUTH_TABLES = (
    "users",
    "organization_memberships",
    "organization_invitations",
    "audit_events",
)


def _validate_legacy_schema(connection) -> None:
    inspector = sa.inspect(connection)
    existing = set(inspector.get_table_names())
    required = {"organizations", *_CONNECTOR_TABLES}
    missing = required - existing
    if missing:
        raise RuntimeError(
            "Cannot migrate tenant identities; missing legacy tables: "
            + ", ".join(sorted(missing))
        )

    existing_auth = set(_AUTH_TABLES) & existing
    if existing_auth:
        raise RuntimeError(
            "Cannot safely create tenant/auth tables that already exist: "
            + ", ".join(sorted(existing_auth))
        )

    expected_columns = {
        "organizations": {"id", "name", "created_at"},
        "integration_connections": {
            "id",
            "organization_id",
            "provider_key",
            "display_name",
            "status",
            "connection_type",
            "config_json",
            "created_at",
        },
        "custom_connectors": {
            "id",
            "organization_id",
            "name",
            "base_url",
            "auth_type",
            "secret_encrypted",
            "secret_label",
            "headers_json",
            "endpoint_path",
            "created_at",
        },
    }
    for table_name, expected in expected_columns.items():
        actual = {column["name"] for column in inspector.get_columns(table_name)}
        if actual != expected:
            raise RuntimeError(
                f"Cannot safely rebuild {table_name}; expected columns "
                f"{sorted(expected)}, found {sorted(actual)}"
            )

    allowed_references = {
        ("integration_connections", "organizations"),
        ("custom_connectors", "organizations"),
    }
    for table_name in existing:
        for foreign_key in inspector.get_foreign_keys(table_name):
            referred = foreign_key.get("referred_table")
            if referred in {"organizations", *_CONNECTOR_TABLES}:
                reference = (table_name, referred)
                is_expected_organization_fk = (
                    reference in allowed_references
                    and foreign_key.get("constrained_columns")
                    == ["organization_id"]
                    and foreign_key.get("referred_columns") == ["id"]
                )
                if not is_expected_organization_fk:
                    raise RuntimeError(
                        "Cannot safely rebuild tenant tables; unexpected foreign "
                        f"key from {table_name} to {referred}"
                    )

    for table_name in _CONNECTOR_TABLES:
        foreign_keys = inspector.get_foreign_keys(table_name)
        if not any(
            foreign_key["constrained_columns"] == ["organization_id"]
            and foreign_key["referred_table"] == "organizations"
            and foreign_key["referred_columns"] == ["id"]
            for foreign_key in foreign_keys
        ):
            raise RuntimeError(
                f"Cannot safely migrate {table_name}; its organization foreign "
                "key is missing or unexpected"
            )

    for table_name in _CONNECTOR_TABLES:
        missing_reference = connection.execute(
            sa.text(
                f"SELECT child.id FROM {table_name} AS child "
                "LEFT JOIN organizations AS parent "
                "ON parent.id = child.organization_id "
                "WHERE parent.id IS NULL LIMIT 1"
            )
        ).first()
        if missing_reference is not None:
            raise RuntimeError(
                f"Cannot migrate {table_name}; row {missing_reference[0]!r} "
                "references a missing organization"
            )


def _organization_uuids(connection):
    organizations = sa.Table(
        "organizations",
        sa.MetaData(),
        autoload_with=connection,
    )
    rows = connection.execute(sa.select(organizations)).mappings().all()
    id_map = {}
    converted_rows = []
    allocated = set()
    for row in rows:
        old_id = row["id"]
        try:
            new_id = UUID(old_id)
        except (ValueError, TypeError, AttributeError):
            new_id = uuid4()
        if new_id in allocated:
            raise RuntimeError(
                "Cannot migrate organizations; UUID identity collision while "
                f"converting legacy id {old_id!r}"
            )
        allocated.add(new_id)
        id_map[old_id] = new_id
        converted_rows.append(
            {
                "id": new_id,
                "legacy_id": old_id,
                "name": row["name"],
                "created_at": row["created_at"],
            }
        )
    return id_map, converted_rows


def _create_replacement_tables() -> None:
    op.create_table(
        "organizations_uuid_new",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("legacy_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("legacy_id"),
    )
    for table_name in _CONNECTOR_TABLES:
        if table_name == "integration_connections":
            columns = [
                sa.Column("id", sa.String(length=36), nullable=False),
                sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
                sa.Column("provider_key", sa.String(length=80), nullable=False),
                sa.Column("display_name", sa.String(length=160), nullable=False),
                sa.Column("status", sa.String(length=32), nullable=False),
                sa.Column("connection_type", sa.String(length=32), nullable=False),
                sa.Column("config_json", sa.JSON(), nullable=False),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            ]
        else:
            columns = [
                sa.Column("id", sa.String(length=36), nullable=False),
                sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
                sa.Column("name", sa.String(length=160), nullable=False),
                sa.Column("base_url", sa.Text(), nullable=False),
                sa.Column("auth_type", sa.String(length=24), nullable=False),
                sa.Column("secret_encrypted", sa.Text(), nullable=True),
                sa.Column("secret_label", sa.String(length=80), nullable=True),
                sa.Column("headers_json", sa.JSON(), nullable=False),
                sa.Column("endpoint_path", sa.String(length=500), nullable=False),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            ]
        op.create_table(
            f"{table_name}_uuid_new",
            *columns,
            sa.ForeignKeyConstraint(
                ["organization_id"],
                ["organizations_uuid_new.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
        )


def _create_auth_tables() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("firebase_uid", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("email_verified", sa.Boolean(), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_firebase_uid", "users", ["firebase_uid"], unique=True)

    op.create_table(
        "organization_memberships",
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'viewer')",
            name="ck_organization_memberships_role",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("user_id", "organization_id"),
    )
    op.create_index(
        "ix_organization_memberships_user_id",
        "organization_memberships",
        ["user_id"],
    )
    op.create_index(
        "ix_organization_memberships_organization_id",
        "organization_memberships",
        ["organization_id"],
    )

    op.create_table(
        "organization_invitations",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("normalized_email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'viewer')",
            name="ck_organization_invitations_role",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_organization_invitations_organization_id",
        "organization_invitations",
        ["organization_id"],
    )
    op.create_index(
        "ix_organization_invitations_created_by_user_id",
        "organization_invitations",
        ["created_by_user_id"],
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("target_type", sa.String(length=120), nullable=True),
        sa.Column("target_id", sa.String(length=128), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("auth_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_actor_user_id", "audit_events", ["actor_user_id"])
    op.create_index(
        "ix_audit_events_organization_id",
        "audit_events",
        ["organization_id"],
    )


def upgrade() -> None:
    connection = op.get_bind()
    _validate_legacy_schema(connection)

    id_map, organizations = _organization_uuids(connection)
    source_tables = {
        table_name: sa.Table(
            table_name,
            sa.MetaData(),
            autoload_with=connection,
        )
        for table_name in _CONNECTOR_TABLES
    }
    connector_rows = {
        table_name: connection.execute(sa.select(table)).mappings().all()
        for table_name, table in source_tables.items()
    }

    foreign_keys_were_enabled = False
    if connection.dialect.name == "sqlite":
        foreign_keys_were_enabled = bool(
            connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
        )
        if foreign_keys_were_enabled:
            with op.get_context().autocommit_block():
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                if connection.exec_driver_sql(
                    "PRAGMA foreign_keys"
                ).scalar():
                    raise RuntimeError(
                        "SQLite foreign-key enforcement could not be disabled "
                        "for the tenant-table rebuild"
                    )

    try:
        _create_replacement_tables()

        organization_table = sa.Table(
            "organizations_uuid_new",
            sa.MetaData(),
            autoload_with=connection,
        )
        if organizations:
            organization_values = organizations
            if connection.dialect.name == "sqlite":
                organization_values = [
                    {**row, "id": row["id"].hex} for row in organizations
                ]
            connection.execute(organization_table.insert(), organization_values)

        for table_name, rows in connector_rows.items():
            replacement = sa.Table(
                f"{table_name}_uuid_new",
                sa.MetaData(),
                autoload_with=connection,
            )
            converted = []
            for row in rows:
                values = dict(row)
                organization_id = id_map[row["organization_id"]]
                values["organization_id"] = (
                    organization_id.hex
                    if connection.dialect.name == "sqlite"
                    else organization_id
                )
                converted.append(values)
            if converted:
                connection.execute(replacement.insert(), converted)

        for table_name in _CONNECTOR_TABLES:
            op.drop_table(table_name)
        op.drop_table("organizations")
        op.rename_table("organizations_uuid_new", "organizations")
        for table_name in _CONNECTOR_TABLES:
            op.rename_table(f"{table_name}_uuid_new", table_name)

        op.create_index(
            "ix_integration_connections_organization_id",
            "integration_connections",
            ["organization_id"],
        )
        op.create_index(
            "ix_integration_connections_provider_key",
            "integration_connections",
            ["provider_key"],
        )
        op.create_index(
            "ix_custom_connectors_organization_id",
            "custom_connectors",
            ["organization_id"],
        )

        _create_auth_tables()

        if connection.dialect.name == "sqlite":
            violations = connection.exec_driver_sql(
                "PRAGMA foreign_key_check"
            ).all()
            if violations:
                raise RuntimeError(
                    "Tenant/auth migration produced SQLite foreign-key violations: "
                    f"{violations!r}"
                )
    finally:
        if foreign_keys_were_enabled:
            with op.get_context().autocommit_block():
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                if not connection.exec_driver_sql(
                    "PRAGMA foreign_keys"
                ).scalar():
                    raise RuntimeError(
                        "SQLite foreign-key enforcement could not be restored "
                        "after the tenant-table rebuild"
                    )


def downgrade() -> None:
    raise NotImplementedError(
        "This migration cannot safely discard user, membership, invitation, or "
        "audit data. Rollback requires restoring a verified pre-migration backup."
    )
