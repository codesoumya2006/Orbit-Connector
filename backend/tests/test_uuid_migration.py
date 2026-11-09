import json
from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import URL, create_engine, inspect, text


BASELINE_REVISION = "0001_existing_schema_baseline"
UUID_MIGRATION_REVISION = "0002_firebase_tenants_and_audit"


def test_uuid_migration_preserves_all_legacy_organization_and_connector_rows():
    config_path = Path.cwd() / "alembic.ini"
    assert config_path.is_file(), (
        f"Expected Alembic config at {config_path} to run the UUID migration"
    )

    database_path = Path.cwd() / f".uuid-migration-test-{uuid4().hex}.db"
    database_url = URL.create("sqlite", database=str(database_path))
    config = Config(str(config_path))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string().replace("%", "%%"),
    )

    engine = None
    organizations = [
        {
            "id": "demo-org",
            "name": "Demo Organization",
            "created_at": "2024-01-02 03:04:05.000000",
        },
        {
            "id": "legacy-org-2",
            "name": "Second Organization",
            "created_at": "2024-02-03 04:05:06.000000",
        },
    ]
    connections = [
        {
            "id": "legacy-connection-1",
            "organization_id": "demo-org",
            "provider_key": "example-provider",
            "display_name": "Demo Connection",
            "status": "active",
            "connection_type": "oauth",
            "config_json": {"region": "west", "enabled": True},
            "created_at": "2024-03-04 05:06:07.000000",
        },
        {
            "id": "legacy-connection-2",
            "organization_id": "legacy-org-2",
            "provider_key": "second-provider",
            "display_name": "Second Connection",
            "status": "paused",
            "connection_type": "api_key",
            "config_json": {"account": "second", "retries": 3},
            "created_at": "2024-04-05 06:07:08.000000",
        },
    ]
    custom_connectors = [
        {
            "id": "legacy-custom-connector-1",
            "organization_id": "demo-org",
            "name": "Demo Custom Connector",
            "base_url": "https://demo.example.test",
            "auth_type": "bearer",
            "secret_encrypted": "dummy-encrypted-secret-for-demo",
            "secret_label": "Demo token",
            "headers_json": {"X-Demo": "first"},
            "endpoint_path": "/v1/items",
            "created_at": "2024-05-06 07:08:09.000000",
        },
        {
            "id": "legacy-custom-connector-2",
            "organization_id": "legacy-org-2",
            "name": "Second Custom Connector",
            "base_url": "https://second.example.test",
            "auth_type": "api_key",
            "secret_encrypted": "dummy-encrypted-secret-for-second",
            "secret_label": "Second key",
            "headers_json": {"X-Second": "second"},
            "endpoint_path": "/v2/resources",
            "created_at": "2024-06-07 08:09:10.000000",
        },
    ]

    try:
        command.upgrade(config, BASELINE_REVISION)
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organizations (id, name, created_at) "
                    "VALUES (:id, :name, :created_at)"
                ),
                organizations,
            )
            connection.execute(
                text(
                    "INSERT INTO integration_connections "
                    "(id, organization_id, provider_key, display_name, status, "
                    "connection_type, config_json, created_at) "
                    "VALUES (:id, :organization_id, :provider_key, :display_name, "
                    ":status, :connection_type, :config_json, :created_at)"
                ),
                [
                    {
                        **row,
                        "config_json": json.dumps(row["config_json"]),
                    }
                    for row in connections
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO custom_connectors "
                    "(id, organization_id, name, base_url, auth_type, "
                    "secret_encrypted, secret_label, headers_json, endpoint_path, "
                    "created_at) VALUES (:id, :organization_id, :name, :base_url, "
                    ":auth_type, :secret_encrypted, :secret_label, :headers_json, "
                    ":endpoint_path, :created_at)"
                ),
                [
                    {
                        **row,
                        "headers_json": json.dumps(row["headers_json"]),
                    }
                    for row in custom_connectors
                ],
            )
        engine.dispose()
        engine = None

        script_directory = ScriptDirectory.from_config(config)
        assert any(
            revision.revision == UUID_MIGRATION_REVISION
            for revision in script_directory.walk_revisions()
        ), (
            f"Cannot locate requested Alembic revision "
            f"'{UUID_MIGRATION_REVISION}' under "
            f"{config_path.parent / 'alembic' / 'versions'}"
        )
        command.upgrade(config, UUID_MIGRATION_REVISION)

        engine = create_engine(database_url)
        with engine.connect() as connection:
            migrated_organizations = connection.execute(
                text("SELECT id, legacy_id, name, created_at FROM organizations")
            ).mappings().all()
            migrated_connections = connection.execute(
                text(
                    "SELECT id, organization_id, provider_key, display_name, status, "
                    "connection_type, config_json, created_at "
                    "FROM integration_connections"
                )
            ).mappings().all()
            migrated_custom_connectors = connection.execute(
                text(
                    "SELECT id, organization_id, name, base_url, auth_type, "
                    "secret_encrypted, secret_label, headers_json, endpoint_path, "
                    "created_at FROM custom_connectors"
                )
            ).mappings().all()
            foreign_key_violations = connection.execute(
                text("PRAGMA foreign_key_check")
            ).all()

        assert len(migrated_organizations) == len(organizations)
        organization_ids_by_legacy_id = {}
        for row in migrated_organizations:
            UUID(row["id"])
            organization_ids_by_legacy_id[row["legacy_id"]] = row["id"]
        assert len({row["id"] for row in migrated_organizations}) == len(organizations)
        assert len(organization_ids_by_legacy_id) == len(organizations)
        assert organization_ids_by_legacy_id.keys() == {
            row["id"] for row in organizations
        }
        for original in organizations:
            migrated = next(
                row
                for row in migrated_organizations
                if row["legacy_id"] == original["id"]
            )
            assert migrated["name"] == original["name"]
            assert migrated["created_at"] == original["created_at"]

        assert len(migrated_connections) == len(connections)
        expected_connections = {row["id"]: row for row in connections}
        for row in migrated_connections:
            original = expected_connections[row["id"]]
            assert row["organization_id"] == organization_ids_by_legacy_id[
                original["organization_id"]
            ]
            for column in (
                "provider_key",
                "display_name",
                "status",
                "connection_type",
                "created_at",
            ):
                assert row[column] == original[column]
            assert json.loads(row["config_json"]) == original["config_json"]

        assert len(migrated_custom_connectors) == len(custom_connectors)
        expected_custom_connectors = {row["id"]: row for row in custom_connectors}
        for row in migrated_custom_connectors:
            original = expected_custom_connectors[row["id"]]
            assert row["organization_id"] == organization_ids_by_legacy_id[
                original["organization_id"]
            ]
            for column in (
                "name",
                "base_url",
                "auth_type",
                "secret_encrypted",
                "secret_label",
                "endpoint_path",
                "created_at",
            ):
                assert row[column] == original[column]
            assert json.loads(row["headers_json"]) == original["headers_json"]

        inspector = inspect(engine)
        for table_name in ("integration_connections", "custom_connectors"):
            assert any(
                foreign_key["constrained_columns"] == ["organization_id"]
                and foreign_key["referred_table"] == "organizations"
                and foreign_key["referred_columns"] == ["id"]
                for foreign_key in inspector.get_foreign_keys(table_name)
            )
        assert foreign_key_violations == []
    finally:
        if engine is not None:
            engine.dispose()
        database_path.unlink(missing_ok=True)
