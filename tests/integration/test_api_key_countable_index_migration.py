"""The countable usage index is additive, retryable, and reversible."""

from datetime import datetime

import pytest
from alembic import command
from sqlalchemy import create_engine, insert, inspect, text

from app.db.migrate import _build_alembic_config, check_schema_drift, run_upgrade
from app.db.migration_url import to_sync_database_url
from app.db.models import Account
from tests.integration.test_affinity_identity_migration import migration_url as migration_url

pytestmark = pytest.mark.integration
PARENT = "20261111_095606_account_usage_countable_index"
REVISION = "20261112_002527_api_key_usage_countable_index"
INDEX = "idx_logs_countable_api_key_time"
COLUMNS = ["requested_at"]


def test_api_key_countable_index_upgrade_downgrade_retry_preserves_history(migration_url):
    run_upgrade(migration_url, PARENT, bootstrap_legacy=False)
    engine = create_engine(to_sync_database_url(migration_url))
    try:
        with engine.begin() as connection:
            connection.execute(
                insert(Account).values(
                    id="index-account",
                    email="index@example.invalid",
                    plan_type="plus",
                    access_token_encrypted=b"fixture",
                    refresh_token_encrypted=b"fixture",
                    id_token_encrypted=b"fixture",
                    last_refresh=datetime(2026, 11, 11),
                )
            )
            connection.execute(
                text(
                    "INSERT INTO request_logs (request_id,account_id,model,status,request_kind,deleted_at,api_key_id) "
                    "VALUES (:request_id,:account_id,'model','success',:kind,:deleted,'key')"
                ),
                [
                    {"request_id": "normal", "account_id": "index-account", "kind": "normal", "deleted": None},
                    {"request_id": "future", "account_id": "index-account", "kind": "future-kind", "deleted": None},
                    {"request_id": "warm", "account_id": "index-account", "kind": "warmup", "deleted": None},
                    {"request_id": "orphan", "account_id": None, "kind": "normal", "deleted": None},
                    {
                        "request_id": "deleted",
                        "account_id": "index-account",
                        "kind": "normal",
                        "deleted": datetime(2026, 11, 1),
                    },
                ],
            )
            before = connection.execute(text("SELECT * FROM request_logs ORDER BY id")).all()
        for _ in range(2):
            run_upgrade(migration_url, "head", bootstrap_legacy=False)
            indexes = {row["name"]: row for row in inspect(engine).get_indexes("request_logs")}
            assert indexes[INDEX]["column_names"] == COLUMNS
            assert check_schema_drift(migration_url) == ()
            with engine.connect() as connection:
                assert connection.execute(text("SELECT * FROM request_logs ORDER BY id")).all() == before
            command.downgrade(_build_alembic_config(migration_url), PARENT)
            assert INDEX not in {row["name"] for row in inspect(engine).get_indexes("request_logs")}
        # An already-built index with the revision still at its parent is a
        # normal interrupted migration; replay must tolerate it.
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE INDEX {INDEX} ON request_logs ({', '.join(COLUMNS)}) "
                    "WHERE api_key_id IS NOT NULL "
                    "AND request_kind NOT IN ('warmup', 'limit_warmup')"
                )
            )
        run_upgrade(migration_url, "head", bootstrap_legacy=False)
        assert check_schema_drift(migration_url) == ()
        with engine.begin() as connection:
            connection.execute(text(f"DROP INDEX {INDEX}"))
        assert any(INDEX in diff for diff in check_schema_drift(migration_url))
    finally:
        engine.dispose()


def test_api_key_countable_index_repairs_invalid_postgresql_build(migration_url):
    if not migration_url.startswith("postgresql"):
        pytest.skip("PostgreSQL concurrent-index recovery")
    run_upgrade(migration_url, PARENT, bootstrap_legacy=False)
    engine = create_engine(to_sync_database_url(migration_url))
    try:
        with engine.begin() as connection:
            connection.execute(text(f"CREATE INDEX {INDEX} ON request_logs (requested_at)"))
            connection.execute(
                text("UPDATE pg_index SET indisvalid=false WHERE indexrelid=to_regclass(:name)"), {"name": INDEX}
            )
        run_upgrade(migration_url, "head", bootstrap_legacy=False)
        indexes = {row["name"]: row for row in inspect(engine).get_indexes("request_logs")}
        assert indexes[INDEX]["column_names"] == COLUMNS
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass(:name)"), {"name": INDEX}
            ).scalar_one()
        assert check_schema_drift(migration_url) == ()
    finally:
        engine.dispose()
