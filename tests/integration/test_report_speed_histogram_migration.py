from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.db.migrate import _build_alembic_config, check_schema_drift, run_upgrade
from app.db.migration_url import to_sync_database_url
from tests.integration.test_affinity_identity_migration import migration_url as migration_url

pytestmark = pytest.mark.integration
_PARENT = "20261110_053001_add_conversation_summaries"
_REVISION = "20261110_053002_add_report_speed_histograms"


def test_speed_histogram_migration_leaves_history_for_background_fold(migration_url: str):
    run_upgrade(migration_url, _PARENT, bootstrap_legacy=False)
    engine = create_engine(to_sync_database_url(migration_url))
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO request_logs (request_id, model, status) VALUES ('preserved', 'model', 'success')")
            )
            before = list(connection.execute(text("SELECT * FROM request_logs")).mappings())
        run_upgrade(migration_url, _REVISION, bootstrap_legacy=False)
        with engine.begin() as connection:
            columns = {
                column["name"]: column for column in inspect(connection).get_columns("account_usage_rollup_state")
            }
            assert columns["request_logs_pruned_through"]["nullable"]
            assert connection.scalar(text("SELECT request_logs_pruned_through FROM account_usage_rollup_state")) is None
            assert connection.scalar(text("SELECT count(*) FROM request_report_speed_histograms")) == 0
            assert list(connection.execute(text("SELECT * FROM request_logs")).mappings()) == before
            connection.execute(
                text("""
                INSERT INTO request_report_speed_histograms
                (bucket_epoch,account_id,api_key_id,model,useragent_group,metric,bin_index,sample_count)
                VALUES (0,'account','key','model','agent','ttft',10,5)
            """)
            )
        command.downgrade(_build_alembic_config(migration_url), _PARENT)
        assert not inspect(engine).has_table("request_report_speed_histograms")
        assert "speed_folded_through" not in {
            column["name"] for column in inspect(engine).get_columns("account_usage_rollup_state")
        }
        assert "request_logs_pruned_through" not in {
            column["name"] for column in inspect(engine).get_columns("account_usage_rollup_state")
        }
        with engine.connect() as connection:
            assert list(connection.execute(text("SELECT * FROM request_logs")).mappings()) == before
        run_upgrade(migration_url, "head", bootstrap_legacy=False)
        assert check_schema_drift(migration_url) == ()
    finally:
        engine.dispose()
