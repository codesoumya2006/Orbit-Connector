from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, inspect, text


BASELINE_REVISION = "0001_existing_schema_baseline"


def test_baseline_migration_creates_legacy_schema():
    config_path = Path.cwd() / "alembic.ini"
    assert config_path.is_file(), (
        f"Expected Alembic config at {config_path} to run the baseline migration"
    )

    database_path = Path.cwd() / f".baseline-test-{uuid4().hex}.db"
    database_url = URL.create("sqlite", database=str(database_path))
    config = Config(str(config_path))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string().replace("%", "%%"),
    )

    engine = None
    try:
        command.upgrade(config, BASELINE_REVISION)
        engine = create_engine(database_url)
        inspector = inspect(engine)
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

        assert set(expected_columns).issubset(inspector.get_table_names())
        for table_name, columns in expected_columns.items():
            assert columns.issubset(
                {column["name"] for column in inspector.get_columns(table_name)}
            )

        for table_name in ("integration_connections", "custom_connectors"):
            assert any(
                foreign_key["constrained_columns"] == ["organization_id"]
                and foreign_key["referred_table"] == "organizations"
                and foreign_key["referred_columns"] == ["id"]
                for foreign_key in inspector.get_foreign_keys(table_name)
            )

        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert revision == BASELINE_REVISION
    finally:
        if engine is not None:
            engine.dispose()
        database_path.unlink(missing_ok=True)



SYNC_JOBS_REVISION = "0003_sync_jobs"
UUID_REVISION = "0002_firebase_tenants_and_audit"


def test_sync_jobs_migration_upgrade_and_downgrade():
    config_path = Path.cwd() / "alembic.ini"
    database_path = Path.cwd() / f".sync-jobs-migration-test-{uuid4().hex}.db"
    database_url = URL.create("sqlite", database=str(database_path))
    config = Config(str(config_path))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string().replace("%", "%%"),
    )

    engine = None
    try:
        # Upgrade to 0003
        command.upgrade(config, SYNC_JOBS_REVISION)
        engine = create_engine(database_url)
        inspector = inspect(engine)

        assert "sync_jobs" in inspector.get_table_names()
        columns = {col["name"] for col in inspector.get_columns("sync_jobs")}
        expected_cols = {
            "id",
            "organization_id",
            "connection_id",
            "requested_by_user_id",
            "job_type",
            "status",
            "idempotency_key",
            "attempt_count",
            "max_attempts",
            "created_at",
            "updated_at",
            "started_at",
            "completed_at",
            "next_retry_at",
            "lease_token",
            "lease_expires_at",
            "metadata",
            "result",
            "error",
            "correlation_id",
        }
        assert expected_cols.issubset(columns)

        with engine.connect() as connection:
            rev = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert rev == SYNC_JOBS_REVISION

        # Downgrade to 0002
        command.downgrade(config, UUID_REVISION)
        inspector_down = inspect(engine)
        assert "sync_jobs" not in inspector_down.get_table_names()

        with engine.connect() as connection:
            rev_down = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert rev_down == UUID_REVISION
    finally:
        if engine is not None:
            engine.dispose()
        database_path.unlink(missing_ok=True)



GA4_REVISION = "0004_ga4_integration"


def test_ga4_integration_migration_upgrade_and_downgrade():
    config_path = Path.cwd() / "alembic.ini"
    database_path = Path.cwd() / f".ga4-migration-test-{uuid4().hex}.db"
    database_url = URL.create("sqlite", database=str(database_path))
    config = Config(str(config_path))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string().replace("%", "%%"),
    )

    engine = None
    try:
        # Upgrade all the way to 0004
        command.upgrade(config, GA4_REVISION)
        engine = create_engine(database_url)
        inspector = inspect(engine)

        assert "oauth_states" in inspector.get_table_names()
        assert "ga4_report_rows" in inspector.get_table_names()

        # Check oauth_states columns
        oauth_cols = {c["name"] for c in inspector.get_columns("oauth_states")}
        assert {"state_token", "code_verifier", "organization_id", "user_id", "connection_id", "expires_at", "used_at", "created_at"}.issubset(oauth_cols)

        # Check ga4_report_rows columns
        ga4_cols = {c["name"] for c in inspector.get_columns("ga4_report_rows")}
        assert {"id", "organization_id", "connection_id", "property_id", "report_date", "channel_group", "sessions", "active_users", "event_count", "sync_job_id"}.issubset(ga4_cols)

        with engine.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert rev == GA4_REVISION

        # Downgrade to 0003
        command.downgrade(config, SYNC_JOBS_REVISION)
        inspector_down = inspect(engine)
        assert "oauth_states" not in inspector_down.get_table_names()
        assert "ga4_report_rows" not in inspector_down.get_table_names()
        assert "sync_jobs" in inspector_down.get_table_names()

        with engine.connect() as conn:
            rev_down = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert rev_down == SYNC_JOBS_REVISION

        # Re-upgrade to 0004
        command.upgrade(config, GA4_REVISION)
        inspector_reup = inspect(engine)
        assert "oauth_states" in inspector_reup.get_table_names()
        assert "ga4_report_rows" in inspector_reup.get_table_names()
    finally:
        if engine is not None:
            engine.dispose()
        database_path.unlink(missing_ok=True)



EXPAND_VERIFIER_REVISION = "0005_expand_oauth_verifier"


def test_expand_oauth_verifier_migration():
    config_path = Path.cwd() / "alembic.ini"
    database_path = Path.cwd() / f".verifier-migration-test-{uuid4().hex}.db"
    database_url = URL.create("sqlite", database=str(database_path))
    config = Config(str(config_path))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string().replace("%", "%%"),
    )

    engine = None
    try:
        # 1. Upgrade to 0004
        command.upgrade(config, GA4_REVISION)
        engine = create_engine(database_url)

        # 2. Seed pre-existing data with 128-char verifier
        test_org_id = uuid4().hex
        test_user_id = uuid4().hex
        test_conn_id = "conn-oauth-1"
        test_state_128 = "state-legacy-1"
        legacy_verifier_128 = "v" * 128

        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO organizations (id, name, created_at) VALUES (:id, 'Org', CURRENT_TIMESTAMP)"),
                {"id": test_org_id},
            )
            conn.execute(
                text("INSERT INTO users (id, firebase_uid, email, email_verified, created_at) VALUES (:id, 'fuid-1', 'test@orbit.test', 1, CURRENT_TIMESTAMP)"),
                {"id": test_user_id},
            )
            conn.execute(
                text("INSERT INTO integration_connections (id, organization_id, provider_key, display_name, status, connection_type, config_json, created_at) VALUES (:id, :org_id, 'google_analytics', 'GA4', 'active', 'oauth', '{}', CURRENT_TIMESTAMP)"),
                {"id": test_conn_id, "org_id": test_org_id},
            )
            conn.execute(
                text("INSERT INTO oauth_states (state_token, code_verifier, organization_id, user_id, connection_id, expires_at, created_at) VALUES (:st, :cv, :org, :usr, :conn, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
                {"st": test_state_128, "cv": legacy_verifier_128, "org": test_org_id, "usr": test_user_id, "conn": test_conn_id},
            )

        # 3. Upgrade to 0005_expand_oauth_verifier
        command.upgrade(config, EXPAND_VERIFIER_REVISION)

        inspector = inspect(engine)
        col_info = {col["name"]: col for col in inspector.get_columns("oauth_states")}
        assert "code_verifier" in col_info

        # 4. Verify existing 128-character row was preserved intact
        with engine.connect() as conn:
            preserved_cv = conn.execute(
                text("SELECT code_verifier FROM oauth_states WHERE state_token = :st"),
                {"st": test_state_128},
            ).scalar_one()
            assert preserved_cv == legacy_verifier_128

        # 5. Insert new 204-character encrypted Fernet verifier
        encrypted_verifier_204 = "gAAAAAB" + ("x" * 197)
        assert len(encrypted_verifier_204) == 204
        test_state_204 = "state-fernet-encrypted"

        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO oauth_states (state_token, code_verifier, organization_id, user_id, connection_id, expires_at, created_at) VALUES (:st, :cv, :org, :usr, :conn, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
                {"st": test_state_204, "cv": encrypted_verifier_204, "org": test_org_id, "usr": test_user_id, "conn": test_conn_id},
            )

        with engine.connect() as conn:
            stored_encrypted = conn.execute(
                text("SELECT code_verifier FROM oauth_states WHERE state_token = :st"),
                {"st": test_state_204},
            ).scalar_one()
            assert stored_encrypted == encrypted_verifier_204

        # 6. Attempt downgrade while long encrypted verifier exists: must refuse and raise RuntimeError
        import pytest
        with pytest.raises(Exception) as exc_info:
            command.downgrade(config, GA4_REVISION)
        assert "cannot downgrade" in str(exc_info.value).lower() or "exceeding 128" in str(exc_info.value).lower()

        # 7. Clean up the long encrypted row (simulating expiry cleanup)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM oauth_states WHERE state_token = :st"), {"st": test_state_204})

        # 8. Downgrade to 0004 succeeds cleanly when no rows exceed 128 chars
        command.downgrade(config, GA4_REVISION)

        with engine.connect() as conn:
            rev_down = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert rev_down == GA4_REVISION
            legacy_still_there = conn.execute(
                text("SELECT code_verifier FROM oauth_states WHERE state_token = :st"),
                {"st": test_state_128},
            ).scalar_one()
            assert legacy_still_there == legacy_verifier_128

        # 9. Re-upgrade back to 0005
        command.upgrade(config, EXPAND_VERIFIER_REVISION)
        with engine.connect() as conn:
            rev_up = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert rev_up == EXPAND_VERIFIER_REVISION

    finally:
        if engine is not None:
            engine.dispose()
        database_path.unlink(missing_ok=True)





def test_sync_jobs_constraints_indexes_and_data_preservation():
    config_path = Path.cwd() / "alembic.ini"
    database_path = Path.cwd() / f".sync-jobs-constraints-test-{uuid4().hex}.db"
    database_url = URL.create("sqlite", database=str(database_path))
    config = Config(str(config_path))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string().replace("%", "%%"),
    )

    engine = None
    try:
        # 1. Step 1: Upgrade to 0002 (baseline + UUID/tenant migration)
        command.upgrade(config, UUID_REVISION)
        engine = create_engine(database_url)

        # 2. Seed tenant, user, and connection data
        test_org_id = uuid4().hex
        test_user_id = uuid4().hex
        test_conn_id = "conn-preserve-1"

        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO organizations (id, name, created_at) "
                    "VALUES (:id, :name, CURRENT_TIMESTAMP)"
                ),
                {"id": test_org_id, "name": "Preserved Org"},
            )
            conn.execute(
                text(
                    "INSERT INTO users (id, firebase_uid, email, email_verified, created_at) "
                    "VALUES (:id, :fuid, :email, 1, CURRENT_TIMESTAMP)"
                ),
                {"id": test_user_id, "fuid": "fuid-preserve", "email": "preserve@orbit.test"},
            )
            conn.execute(
                text(
                    "INSERT INTO integration_connections (id, organization_id, provider_key, display_name, status, connection_type, config_json, created_at) "
                    "VALUES (:id, :org_id, 'google_analytics', 'GA4 Preserved', 'active', 'oauth', '{}', CURRENT_TIMESTAMP)"
                ),
                {"id": test_conn_id, "org_id": test_org_id},
            )

        # 3. Upgrade to 0003_sync_jobs
        command.upgrade(config, SYNC_JOBS_REVISION)

        inspector = inspect(engine)
        assert "sync_jobs" in inspector.get_table_names()

        # Verify foreign keys
        fks = inspector.get_foreign_keys("sync_jobs")
        fk_map = {tuple(fk["constrained_columns"]): (fk["referred_table"], tuple(fk["referred_columns"])) for fk in fks}
        assert ("organization_id",) in fk_map
        assert fk_map[("organization_id",)][0] == "organizations"
        assert ("connection_id",) in fk_map
        assert fk_map[("connection_id",)][0] == "integration_connections"
        assert ("requested_by_user_id",) in fk_map
        assert fk_map[("requested_by_user_id",)][0] == "users"

        # Verify indexes
        indexes = {idx["name"]: tuple(idx["column_names"]) for idx in inspector.get_indexes("sync_jobs")}
        assert "ix_sync_jobs_organization_id" in indexes
        assert "ix_sync_jobs_connection_id" in indexes
        assert "ix_sync_jobs_requested_by_user_id" in indexes
        assert "ix_sync_jobs_status" in indexes
        assert "ix_sync_jobs_idempotency_key" in indexes
        assert "ix_sync_jobs_next_retry_at" in indexes
        assert "ix_sync_jobs_lease_expires_at" in indexes

        # Verify data preservation of pre-existing tables
        with engine.connect() as conn:
            org_row = conn.execute(
                text("SELECT name FROM organizations WHERE id = :id"),
                {"id": test_org_id},
            ).one()
            assert org_row[0] == "Preserved Org"

            conn_row = conn.execute(
                text("SELECT display_name FROM integration_connections WHERE id = :id"),
                {"id": test_conn_id},
            ).one()
            assert conn_row[0] == "GA4 Preserved"

        # 4. Insert a job into sync_jobs
        test_job_id = uuid4().hex
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sync_jobs (id, organization_id, connection_id, requested_by_user_id, job_type, status, idempotency_key, metadata, result, error) "
                    "VALUES (:id, :org_id, :conn_id, :user_id, 'full_sync', 'queued', 'preserve-key', '{}', '{}', '{}')"
                ),
                {"id": test_job_id, "org_id": test_org_id, "conn_id": test_conn_id, "user_id": test_user_id},
            )

        # 5. Downgrade back to 0002
        command.downgrade(config, UUID_REVISION)

        # Confirm sync_jobs is dropped but organizations and connections remain intact
        inspector_down = inspect(engine)
        assert "sync_jobs" not in inspector_down.get_table_names()

        with engine.connect() as conn:
            org_check = conn.execute(
                text("SELECT name FROM organizations WHERE id = :id"),
                {"id": test_org_id},
            ).scalar_one()
            assert org_check == "Preserved Org"

            conn_check = conn.execute(
                text("SELECT display_name FROM integration_connections WHERE id = :id"),
                {"id": test_conn_id},
            ).scalar_one()
            assert conn_check == "GA4 Preserved"

        # 6. Re-upgrade back to 0003
        command.upgrade(config, SYNC_JOBS_REVISION)
        inspector_reup = inspect(engine)
        assert "sync_jobs" in inspector_reup.get_table_names()

        with engine.connect() as conn:
            org_final = conn.execute(
                text("SELECT name FROM organizations WHERE id = :id"),
                {"id": test_org_id},
            ).scalar_one()
            assert org_final == "Preserved Org"

    finally:
        if engine is not None:
            engine.dispose()
        database_path.unlink(missing_ok=True)



GA4_REVISION = "0004_ga4_integration"


def test_ga4_integration_migration_upgrade_and_downgrade():
    config_path = Path.cwd() / "alembic.ini"
    database_path = Path.cwd() / f".ga4-migration-test-{uuid4().hex}.db"
    database_url = URL.create("sqlite", database=str(database_path))
    config = Config(str(config_path))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string().replace("%", "%%"),
    )

    engine = None
    try:
        # Upgrade all the way to 0004
        command.upgrade(config, GA4_REVISION)
        engine = create_engine(database_url)
        inspector = inspect(engine)

        assert "oauth_states" in inspector.get_table_names()
        assert "ga4_report_rows" in inspector.get_table_names()

        # Check oauth_states columns
        oauth_cols = {c["name"] for c in inspector.get_columns("oauth_states")}
        assert {"state_token", "code_verifier", "organization_id", "user_id", "connection_id", "expires_at", "used_at", "created_at"}.issubset(oauth_cols)

        # Check ga4_report_rows columns
        ga4_cols = {c["name"] for c in inspector.get_columns("ga4_report_rows")}
        assert {"id", "organization_id", "connection_id", "property_id", "report_date", "channel_group", "sessions", "active_users", "event_count", "sync_job_id"}.issubset(ga4_cols)

        with engine.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert rev == GA4_REVISION

        # Downgrade to 0003
        command.downgrade(config, SYNC_JOBS_REVISION)
        inspector_down = inspect(engine)
        assert "oauth_states" not in inspector_down.get_table_names()
        assert "ga4_report_rows" not in inspector_down.get_table_names()
        assert "sync_jobs" in inspector_down.get_table_names()

        with engine.connect() as conn:
            rev_down = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert rev_down == SYNC_JOBS_REVISION

        # Re-upgrade to 0004
        command.upgrade(config, GA4_REVISION)
        inspector_reup = inspect(engine)
        assert "oauth_states" in inspector_reup.get_table_names()
        assert "ga4_report_rows" in inspector_reup.get_table_names()
    finally:
        if engine is not None:
            engine.dispose()
        database_path.unlink(missing_ok=True)



EXPAND_VERIFIER_REVISION = "0005_expand_oauth_verifier"


def test_expand_oauth_verifier_migration():
    config_path = Path.cwd() / "alembic.ini"
    database_path = Path.cwd() / f".verifier-migration-test-{uuid4().hex}.db"
    database_url = URL.create("sqlite", database=str(database_path))
    config = Config(str(config_path))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string().replace("%", "%%"),
    )

    engine = None
    try:
        # 1. Upgrade to 0004
        command.upgrade(config, GA4_REVISION)
        engine = create_engine(database_url)

        # 2. Seed pre-existing data with 128-char verifier
        test_org_id = uuid4().hex
        test_user_id = uuid4().hex
        test_conn_id = "conn-oauth-1"
        test_state_128 = "state-legacy-1"
        legacy_verifier_128 = "v" * 128

        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO organizations (id, name, created_at) VALUES (:id, 'Org', CURRENT_TIMESTAMP)"),
                {"id": test_org_id},
            )
            conn.execute(
                text("INSERT INTO users (id, firebase_uid, email, email_verified, created_at) VALUES (:id, 'fuid-1', 'test@orbit.test', 1, CURRENT_TIMESTAMP)"),
                {"id": test_user_id},
            )
            conn.execute(
                text("INSERT INTO integration_connections (id, organization_id, provider_key, display_name, status, connection_type, config_json, created_at) VALUES (:id, :org_id, 'google_analytics', 'GA4', 'active', 'oauth', '{}', CURRENT_TIMESTAMP)"),
                {"id": test_conn_id, "org_id": test_org_id},
            )
            conn.execute(
                text("INSERT INTO oauth_states (state_token, code_verifier, organization_id, user_id, connection_id, expires_at, created_at) VALUES (:st, :cv, :org, :usr, :conn, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
                {"st": test_state_128, "cv": legacy_verifier_128, "org": test_org_id, "usr": test_user_id, "conn": test_conn_id},
            )

        # 3. Upgrade to 0005_expand_oauth_verifier
        command.upgrade(config, EXPAND_VERIFIER_REVISION)

        inspector = inspect(engine)
        col_info = {col["name"]: col for col in inspector.get_columns("oauth_states")}
        assert "code_verifier" in col_info

        # 4. Verify existing 128-character row was preserved intact
        with engine.connect() as conn:
            preserved_cv = conn.execute(
                text("SELECT code_verifier FROM oauth_states WHERE state_token = :st"),
                {"st": test_state_128},
            ).scalar_one()
            assert preserved_cv == legacy_verifier_128

        # 5. Insert new 204-character encrypted Fernet verifier
        encrypted_verifier_204 = "gAAAAAB" + ("x" * 197)
        assert len(encrypted_verifier_204) == 204
        test_state_204 = "state-fernet-encrypted"

        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO oauth_states (state_token, code_verifier, organization_id, user_id, connection_id, expires_at, created_at) VALUES (:st, :cv, :org, :usr, :conn, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
                {"st": test_state_204, "cv": encrypted_verifier_204, "org": test_org_id, "usr": test_user_id, "conn": test_conn_id},
            )

        with engine.connect() as conn:
            stored_encrypted = conn.execute(
                text("SELECT code_verifier FROM oauth_states WHERE state_token = :st"),
                {"st": test_state_204},
            ).scalar_one()
            assert stored_encrypted == encrypted_verifier_204

        # 6. Attempt downgrade while long encrypted verifier exists: must refuse and raise RuntimeError
        import pytest
        with pytest.raises(Exception) as exc_info:
            command.downgrade(config, GA4_REVISION)
        assert "cannot downgrade" in str(exc_info.value).lower() or "exceeding 128" in str(exc_info.value).lower()

        # 7. Clean up the long encrypted row (simulating expiry cleanup)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM oauth_states WHERE state_token = :st"), {"st": test_state_204})

        # 8. Downgrade to 0004 succeeds cleanly when no rows exceed 128 chars
        command.downgrade(config, GA4_REVISION)

        with engine.connect() as conn:
            rev_down = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert rev_down == GA4_REVISION
            legacy_still_there = conn.execute(
                text("SELECT code_verifier FROM oauth_states WHERE state_token = :st"),
                {"st": test_state_128},
            ).scalar_one()
            assert legacy_still_there == legacy_verifier_128

        # 9. Re-upgrade back to 0005
        command.upgrade(config, EXPAND_VERIFIER_REVISION)
        with engine.connect() as conn:
            rev_up = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert rev_up == EXPAND_VERIFIER_REVISION

    finally:
        if engine is not None:
            engine.dispose()
        database_path.unlink(missing_ok=True)






GOOGLE_ADS_REVISION = "0006_google_ads_integration"


def test_google_ads_migration_upgrade_and_downgrade():
    config_path = Path.cwd() / "alembic.ini"
    database_path = Path.cwd() / f".google-ads-migration-test-{uuid4().hex}.db"
    database_url = URL.create("sqlite", database=str(database_path))
    config = Config(str(config_path))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string().replace("%", "%%"),
    )

    engine = None
    try:
        # Upgrade all the way to 0006_google_ads_integration
        command.upgrade(config, GOOGLE_ADS_REVISION)
        engine = create_engine(database_url)
        inspector = inspect(engine)

        assert "google_ads_campaign_reports" in inspector.get_table_names()
        columns = {c["name"] for c in inspector.get_columns("google_ads_campaign_reports")}
        expected_cols = {
            "id",
            "organization_id",
            "connection_id",
            "customer_id",
            "report_date",
            "campaign_id",
            "campaign_name",
            "campaign_status",
            "impressions",
            "clicks",
            "cost_micros",
            "cost",
            "conversions",
            "conversions_value",
            "sync_job_id",
            "created_at",
            "updated_at",
        }
        assert expected_cols.issubset(columns)

        with engine.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert rev == GOOGLE_ADS_REVISION

        # Downgrade to 0005
        command.downgrade(config, EXPAND_VERIFIER_REVISION)
        inspector = inspect(engine)
        assert "google_ads_campaign_reports" not in inspector.get_table_names()

        with engine.connect() as conn:
            rev_down = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert rev_down == EXPAND_VERIFIER_REVISION

        # Re-upgrade to 0006
        command.upgrade(config, GOOGLE_ADS_REVISION)
        inspector = inspect(engine)
        assert "google_ads_campaign_reports" in inspector.get_table_names()

    finally:
        if engine is not None:
            engine.dispose()
        database_path.unlink(missing_ok=True)



GOOGLE_ADS_NUMERIC_REVISION = "0007_google_ads_cost_numeric"


def test_google_ads_numeric_cost_migration_with_seeded_data():
    """Verify 0007 migration: upgrade with seeded float rows, downgrade, and re-upgrade preserving precision."""
    config_path = Path.cwd() / "alembic.ini"
    database_path = Path.cwd() / f".google-ads-numeric-migration-test-{uuid4().hex}.db"
    database_url = URL.create("sqlite", database=str(database_path))
    config = Config(str(config_path))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string().replace("%", "%%"),
    )

    engine = None
    try:
        # 1. Upgrade to 0006_google_ads_integration (where cost column is Float)
        command.upgrade(config, GOOGLE_ADS_REVISION)
        engine = create_engine(database_url)
        inspector = inspect(engine)

        col_map_0006 = {c["name"]: str(c["type"]).upper() for c in inspector.get_columns("google_ads_campaign_reports")}
        assert "FLOAT" in col_map_0006["cost"]

        # 2. Seed organization, connection, and report rows
        org_id = uuid4().hex
        conn_id = "test-conn-numeric-mig"
        row1_id = uuid4().hex
        row2_id = uuid4().hex
        row3_id = uuid4().hex

        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO organizations (id, name, created_at) VALUES (:id, :name, CURRENT_TIMESTAMP)"),
                {"id": org_id, "name": "Numeric Test Org"},
            )
            conn.execute(
                text(
                    "INSERT INTO integration_connections (id, organization_id, provider_key, display_name, status, connection_type, config_json, created_at) "
                    "VALUES (:id, :org, :pk, :dn, :st, :ct, :cfg, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": conn_id,
                    "org": org_id,
                    "pk": "google_ads",
                    "dn": "Google Ads Test",
                    "st": "connected",
                    "ct": "oauth",
                    "cfg": "{}",
                },
            )
            # Insert seeded report rows with float values
            conn.execute(
                text(
                    "INSERT INTO google_ads_campaign_reports "
                    "(id, organization_id, connection_id, customer_id, report_date, campaign_id, campaign_name, campaign_status, impressions, clicks, cost_micros, cost, conversions, conversions_value, created_at, updated_at) "
                    "VALUES (:id, :org, :conn, :cust, :dt, :cid, :cname, :cst, :imp, :clk, :micros, :cost, :conv, :cval, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                [
                    {
                        "id": row1_id,
                        "org": org_id,
                        "conn": conn_id,
                        "cust": "1234567890",
                        "dt": "2025-01-01",
                        "cid": "camp-1",
                        "cname": "Camp 1",
                        "cst": "ENABLED",
                        "imp": 100,
                        "clk": 10,
                        "micros": 12345678,
                        "cost": 12.345678,
                        "conv": 1.0,
                        "cval": 10.0,
                    },
                    {
                        "id": row2_id,
                        "org": org_id,
                        "conn": conn_id,
                        "cust": "1234567890",
                        "dt": "2025-01-02",
                        "cid": "camp-2",
                        "cname": "Camp 2",
                        "cst": "ENABLED",
                        "imp": 50,
                        "clk": 5,
                        "micros": 1,
                        "cost": 0.000001,
                        "conv": 0.0,
                        "cval": 0.0,
                    },
                    {
                        "id": row3_id,
                        "org": org_id,
                        "conn": conn_id,
                        "cust": "1234567890",
                        "dt": "2025-01-03",
                        "cid": "camp-3",
                        "cname": "Camp 3",
                        "cst": "PAUSED",
                        "imp": 500,
                        "clk": 25,
                        "micros": 999999500000,
                        "cost": 999999.5,
                        "conv": 5.0,
                        "cval": 50.0,
                    },
                ],
            )

        # 3. Upgrade to 0007_google_ads_cost_numeric
        command.upgrade(config, GOOGLE_ADS_NUMERIC_REVISION)

        inspector_0007 = inspect(engine)
        col_map_0007 = {c["name"]: str(c["type"]).upper() for c in inspector_0007.get_columns("google_ads_campaign_reports")}
        assert "NUMERIC" in col_map_0007["cost"]

        with engine.connect() as conn:
            rev_0007 = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert rev_0007 == GOOGLE_ADS_NUMERIC_REVISION

            # 4. Verify seeded data is preserved across upgrade
            rows = conn.execute(
                text("SELECT id, cost_micros, cost FROM google_ads_campaign_reports ORDER BY report_date ASC")
            ).fetchall()
            assert len(rows) == 3
            assert rows[0][1] == 12345678
            assert rows[1][1] == 1
            assert rows[2][1] == 999999500000

        # 5. Downgrade to 0006_google_ads_integration
        command.downgrade(config, GOOGLE_ADS_REVISION)

        inspector_down = inspect(engine)
        col_map_down = {c["name"]: str(c["type"]).upper() for c in inspector_down.get_columns("google_ads_campaign_reports")}
        assert "FLOAT" in col_map_down["cost"]

        with engine.connect() as conn:
            rev_down = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert rev_down == GOOGLE_ADS_REVISION

            # Seeded data preserved across downgrade
            rows_down = conn.execute(
                text("SELECT count(*) FROM google_ads_campaign_reports")
            ).scalar_one()
            assert rows_down == 3

        # 6. Re-upgrade to 0007_google_ads_cost_numeric
        command.upgrade(config, GOOGLE_ADS_NUMERIC_REVISION)

        inspector_reup = inspect(engine)
        col_map_reup = {c["name"]: str(c["type"]).upper() for c in inspector_reup.get_columns("google_ads_campaign_reports")}
        assert "NUMERIC" in col_map_reup["cost"]

        with engine.connect() as conn:
            rev_reup = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert rev_reup == GOOGLE_ADS_NUMERIC_REVISION
            rows_reup = conn.execute(
                text("SELECT count(*) FROM google_ads_campaign_reports")
            ).scalar_one()
            assert rows_reup == 3

    finally:
        if engine is not None:
            engine.dispose()
        database_path.unlink(missing_ok=True)


META_ADS_REVISION = "0008_meta_ads_integration"


def test_meta_ads_migration_upgrade_and_downgrade():
    config_path = Path.cwd() / "alembic.ini"
    database_path = Path.cwd() / f".meta-ads-migration-test-{uuid4().hex}.db"
    database_url = URL.create("sqlite", database=str(database_path))
    config = Config(str(config_path))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string().replace("%", "%%"),
    )

    engine = None
    try:
        # 1. Upgrade up to 0007
        command.upgrade(config, GOOGLE_ADS_NUMERIC_REVISION)

        engine = create_engine(database_url)
        inspector_0007 = inspect(engine)
        assert "meta_ads_campaign_reports" not in inspector_0007.get_table_names()

        # 2. Upgrade to 0008
        command.upgrade(config, META_ADS_REVISION)

        inspector_0008 = inspect(engine)
        assert "meta_ads_campaign_reports" in inspector_0008.get_table_names()
        columns = {c["name"]: str(c["type"]).upper() for c in inspector_0008.get_columns("meta_ads_campaign_reports")}
        assert "NUMERIC" in columns["spend"]
        assert "NUMERIC" in columns["cpc"]
        assert "NUMERIC" in columns["cpm"]
        assert "FLOAT" in columns["ctr"]
        assert "FLOAT" in columns["frequency"]
        assert "FLOAT" in columns["conversions"]
        assert "NUMERIC" in columns["conversions_value"]
        assert "INTEGER" in columns["impressions"] or "BIGINT" in columns["impressions"]
        assert "INTEGER" in columns["reach"] or "BIGINT" in columns["reach"]
        assert "INTEGER" in columns["clicks"] or "BIGINT" in columns["clicks"]

        with engine.connect() as conn:
            rev_0008 = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert rev_0008 == META_ADS_REVISION

            # Seed a test row
            conn.execute(
                text(
                    """
                    INSERT INTO meta_ads_campaign_reports (
                        id, organization_id, connection_id, ad_account_id, report_date,
                        campaign_id, campaign_name, campaign_status, impressions, reach, clicks,
                        spend, ctr, cpc, cpm, frequency, conversions, conversions_value,
                        created_at, updated_at
                    ) VALUES (
                        '00000000-0000-0000-0000-000000000009', '00000000-0000-0000-0000-000000000001', 'meta-conn-1',
                        '123456789', '2026-11-01', 'camp-1', 'Test Campaign', 'ACTIVE',
                        1000, 800, 50, 12.340000, 5.0, 0.2468, 12.34, 1.25, 5, 50.00,
                        '2026-11-09T00:00:00', '2026-11-09T00:00:00'
                    )
                    """
                )
            )
            conn.commit()
            count = conn.execute(text("SELECT count(*) FROM meta_ads_campaign_reports")).scalar_one()
            assert count == 1

        # 3. Downgrade back to 0007
        command.downgrade(config, GOOGLE_ADS_NUMERIC_REVISION)

        inspector_down = inspect(engine)
        assert "meta_ads_campaign_reports" not in inspector_down.get_table_names()

        with engine.connect() as conn:
            rev_down = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert rev_down == GOOGLE_ADS_NUMERIC_REVISION

        # 4. Re-upgrade to 0008
        command.upgrade(config, META_ADS_REVISION)

        inspector_reup = inspect(engine)
        assert "meta_ads_campaign_reports" in inspector_reup.get_table_names()
        with engine.connect() as conn:
            rev_reup = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert rev_reup == META_ADS_REVISION

    finally:
        if engine is not None:
            engine.dispose()
        database_path.unlink(missing_ok=True)


SUPERMETRICS_GROQ_REVISION = "0009_supermetrics_and_groq_integration"


def test_supermetrics_and_groq_migration_upgrade_and_downgrade():
    """Verify that migration 0009 creates supermetrics_marketing_reports with proper types and can downgrade/re-upgrade."""
    config_path = Path.cwd() / "alembic.ini"
    assert config_path.is_file()

    database_path = Path.cwd() / f".supermetrics-mig-test-{uuid4().hex}.db"
    database_url = URL.create("sqlite", database=str(database_path))
    config = Config(str(config_path))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string().replace("%", "%%"),
    )

    engine = None
    try:
        # 1. Upgrade to 0008
        command.upgrade(config, META_ADS_REVISION)
        engine = create_engine(database_url)

        # 2. Upgrade to 0009
        command.upgrade(config, SUPERMETRICS_GROQ_REVISION)

        inspector_0009 = inspect(engine)
        assert "supermetrics_marketing_reports" in inspector_0009.get_table_names()
        columns = {c["name"]: str(c["type"]).upper() for c in inspector_0009.get_columns("supermetrics_marketing_reports")}
        assert "NUMERIC" in columns["cost"]
        assert "NUMERIC" in columns["conversions_value"]
        assert "FLOAT" in columns["conversions"]
        assert "INTEGER" in columns["impressions"] or "BIGINT" in columns["impressions"]
        assert "INTEGER" in columns["clicks"] or "BIGINT" in columns["clicks"]
        assert "VARCHAR" in columns["ds_id"] or "STRING" in columns["ds_id"] or "TEXT" in columns["ds_id"]

        with engine.connect() as conn:
            rev_0009 = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert rev_0009 == SUPERMETRICS_GROQ_REVISION

            # Seed a test row
            conn.execute(
                text(
                    """
                    INSERT INTO supermetrics_marketing_reports (
                        id, organization_id, connection_id, ds_id, ds_account_id, report_date,
                        campaign_id, campaign_name, impressions, clicks, cost,
                        conversions, conversions_value, currency,
                        created_at, updated_at
                    ) VALUES (
                        '00000000-0000-0000-0000-000000000010', '00000000-0000-0000-0000-000000000001', 'sm-conn-1',
                        'FB', 'act_12345', '2026-11-01', 'camp-sm-1', 'Supermetrics Campaign',
                        1200, 75, 45.670000, 4.0, 180.00, 'USD',
                        '2026-11-09T00:00:00', '2026-11-09T00:00:00'
                    )
                    """
                )
            )
            conn.commit()
            count = conn.execute(text("SELECT count(*) FROM supermetrics_marketing_reports")).scalar_one()
            assert count == 1

        # 3. Downgrade back to 0008
        command.downgrade(config, META_ADS_REVISION)

        inspector_down = inspect(engine)
        assert "supermetrics_marketing_reports" not in inspector_down.get_table_names()

        with engine.connect() as conn:
            rev_down = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert rev_down == META_ADS_REVISION

        # 4. Re-upgrade to 0009
        command.upgrade(config, SUPERMETRICS_GROQ_REVISION)

        inspector_reup = inspect(engine)
        assert "supermetrics_marketing_reports" in inspector_reup.get_table_names()
        with engine.connect() as conn:
            rev_reup = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert rev_reup == SUPERMETRICS_GROQ_REVISION

    finally:
        if engine is not None:
            engine.dispose()
        database_path.unlink(missing_ok=True)


