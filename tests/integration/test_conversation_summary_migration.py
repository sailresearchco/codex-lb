from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.db.migrate import _build_alembic_config, check_schema_drift, run_upgrade
from app.db.migration_url import to_sync_database_url
from tests.integration.test_affinity_identity_migration import migration_url as migration_url

pytestmark = pytest.mark.integration
_PARENT = "20261110_051702_add_upgrade_repair_target"
_REVISION = "20261110_053001_add_conversation_summaries"


def test_conversation_summary_migration_preserves_raw_history_and_round_trips(migration_url):
    run_upgrade(migration_url, _PARENT, bootstrap_legacy=False)
    engine = create_engine(to_sync_database_url(migration_url))
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO request_logs (request_id, model, status, conversation_id) "
                    "VALUES ('preserved', 'model', 'success', 'thread')"
                )
            )
            before = dict(connection.execute(text("SELECT * FROM request_logs")).mappings().one())
        run_upgrade(migration_url, _REVISION, bootstrap_legacy=False)
        with engine.connect() as connection:
            after = dict(connection.execute(text("SELECT * FROM request_logs")).mappings().one())
            assert not after.pop("conversation_summary_included")
            assert after == before
            assert connection.scalar(text("SELECT count(*) FROM request_conversation_summaries")) == 0
        indexes = {index["name"] for index in inspect(engine).get_indexes("request_logs")}
        assert {"idx_logs_conversation_summary_pending", "idx_logs_conversation_summary_time"} <= indexes
        cursor = next(
            column
            for column in inspect(engine).get_columns("account_usage_rollup_state")
            if column["name"] == "conversation_reset_after_id"
        )
        assert cursor["nullable"]
        command.downgrade(_build_alembic_config(migration_url), _PARENT)
        assert "conversation_reset_after_id" not in {
            column["name"] for column in inspect(engine).get_columns("account_usage_rollup_state")
        }
        with engine.connect() as connection:
            assert dict(connection.execute(text("SELECT * FROM request_logs")).mappings().one()) == before
        run_upgrade(migration_url, "head", bootstrap_legacy=False)
        assert check_schema_drift(migration_url) == ()
    finally:
        engine.dispose()
