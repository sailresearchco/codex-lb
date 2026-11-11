from __future__ import annotations

from datetime import datetime

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text

from app.db.migrate import _build_alembic_config, check_schema_drift, run_upgrade
from app.db.migration_url import to_sync_database_url
from tests.integration.test_affinity_identity_migration import migration_url as migration_url

pytestmark = pytest.mark.integration
PARENT = "20261110_053002_add_report_speed_histograms"
REVISION = "20261110_053003_add_thread_identity_cache"
TABLES = (
    "thread_identity_hours",
    "thread_identity_account_hours",
    "thread_identity_pair_hours",
    "thread_identity_dirty_hours",
)


def test_thread_cache_migration_preserves_raw_history_without_backfill(migration_url):
    run_upgrade(migration_url, PARENT, bootstrap_legacy=False)
    engine = create_engine(to_sync_database_url(migration_url))
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO request_logs (request_id,model,status) VALUES ('keep','m','success')"))
            original = list(connection.execute(text("SELECT * FROM request_logs")).mappings())
        run_upgrade(migration_url, REVISION, bootstrap_legacy=False)
        with engine.connect() as connection:
            for table in TABLES:
                assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
            assert list(connection.execute(text("SELECT * FROM request_logs")).mappings()) == original
        command.downgrade(_build_alembic_config(migration_url), PARENT)
        assert all(not inspect(engine).has_table(table) for table in TABLES)
        assert "thread_folded_through" not in {
            row["name"] for row in inspect(engine).get_columns("account_usage_rollup_state")
        }
        run_upgrade(migration_url, "head", bootstrap_legacy=False)
        assert check_schema_drift(migration_url) == ()
    finally:
        engine.dispose()


def test_thread_cache_migration_replay_preserves_caches_and_fold_state(migration_url):
    run_upgrade(migration_url, REVISION, bootstrap_legacy=False)
    engine = create_engine(to_sync_database_url(migration_url))
    preserved_tables = (*TABLES, "request_report_speed_histograms", "account_usage_rollup_state")
    first_at = datetime(2026, 11, 1, 10)
    last_at = datetime(2026, 11, 1, 10, 30)
    bucket_epoch = 1793527200
    keys = {"bucket_epoch": bucket_epoch, "keyed": 1, "thread_key": "thread"}
    try:
        with engine.begin() as connection:
            metadata = MetaData()
            tables = {name: Table(name, metadata, autoload_with=connection) for name in preserved_tables}
            connection.execute(
                tables["thread_identity_hours"]
                .insert()
                .values(
                    **keys,
                    requests=3,
                    cache_input_tokens=300,
                    cache_cached_input_tokens=50,
                    first_at=first_at,
                    first_id=1,
                    first_account="a",
                    first_input=100,
                    first_epoch=bucket_epoch,
                    last_at=last_at,
                    last_id=3,
                    last_account="b",
                    last_input=150,
                    last_epoch=bucket_epoch + 1800,
                )
            )
            connection.execute(
                tables["thread_identity_account_hours"].insert(),
                [{**keys, "account_id": "a", "requests": 2}, {**keys, "account_id": "b", "requests": 1}],
            )
            connection.execute(
                tables["thread_identity_pair_hours"].insert().values(**keys, from_account="a", to_account="b", turns=1)
            )
            connection.execute(
                tables["thread_identity_dirty_hours"].insert().values(bucket_epoch=bucket_epoch, generation=7)
            )
            connection.execute(
                tables["request_report_speed_histograms"]
                .insert()
                .values(
                    bucket_epoch=bucket_epoch,
                    account_id="a",
                    api_key_id="key",
                    model="model",
                    useragent_group="codex",
                    metric="ttft",
                    bin_index=42,
                    sample_count=3,
                )
            )
            state = tables["account_usage_rollup_state"]
            connection.execute(
                state.update()
                .where(state.c.id == 1)
                .values(
                    thread_folded_through=datetime(2026, 11, 2),
                    speed_folded_through=datetime(2026, 11, 3),
                    speed_history_from=datetime(2026, 10, 1),
                    request_logs_pruned_through=datetime(2026, 11, 1),
                )
            )
            before = {
                name: list(connection.execute(select(table).order_by(*table.primary_key)).mappings())
                for name, table in tables.items()
            }
            assert len(before["account_usage_rollup_state"]) == 1
        # Rewind only the ledger: bootstrap recovery must retain the surviving schema and data.
        command.stamp(_build_alembic_config(migration_url), PARENT)
        assert run_upgrade(migration_url, REVISION, bootstrap_legacy=False).current_revision == REVISION
        with engine.connect() as connection:
            for name, table in tables.items():
                assert list(connection.execute(select(table).order_by(*table.primary_key)).mappings()) == before[name]
        run_upgrade(migration_url, "head", bootstrap_legacy=False)
        assert check_schema_drift(migration_url) == ()
    finally:
        engine.dispose()
