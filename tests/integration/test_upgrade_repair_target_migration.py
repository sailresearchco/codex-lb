"""Repair target migration preserves watermarks and existing repair progress."""

from datetime import datetime

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.db.migrate import _build_alembic_config, check_schema_drift, run_upgrade
from app.db.migration_url import to_sync_database_url
from tests.integration.test_affinity_identity_migration import _current_head
from tests.integration.test_affinity_identity_migration import (
    migration_url as migration_url,
)

pytestmark = pytest.mark.integration
_PARENT = "20261109_191616_conversation_history_index"
_REVISION = "20261110_051702_add_upgrade_repair_target"
_TABLE = "account_usage_rollup_state"
_COLUMN = "upgrade_repair_through"


@pytest.mark.parametrize("marker", [None, datetime(2026, 11, 7)])
def test_upgrade_repair_target_migration_preserves_progress_and_round_trips(migration_url: str, marker) -> None:
    run_upgrade(migration_url, _PARENT, bootstrap_legacy=False)
    engine = create_engine(to_sync_database_url(migration_url))
    try:
        with engine.begin() as connection:
            connection.execute(text(f"DELETE FROM {_TABLE}"))
            connection.execute(
                text(f"""
                INSERT INTO {_TABLE} (id, folded_through, hourly_folded_through,
                    conversation_folded_through, reports_folded_through, upgrade_repair_from)
                VALUES (1, :watermark, :watermark, :watermark, :watermark, :marker)
                """),
                {"watermark": datetime(2026, 11, 9), "marker": marker},
            )
            before = dict(connection.execute(text(f"SELECT * FROM {_TABLE}")).mappings().one())

        run_upgrade(migration_url, _REVISION, bootstrap_legacy=False)
        column = next(column for column in inspect(engine).get_columns(_TABLE) if column["name"] == _COLUMN)
        assert column["nullable"]
        assert column["default"] is None
        with engine.begin() as connection:
            after = dict(connection.execute(text(f"SELECT * FROM {_TABLE}")).mappings().one())
            assert after.pop(_COLUMN) is None
            assert after == before
            connection.execute(text(f"UPDATE {_TABLE} SET {_COLUMN} = :target"), {"target": datetime(2026, 11, 9)})

        command.downgrade(_build_alembic_config(migration_url), _PARENT)
        assert _COLUMN not in {column["name"] for column in inspect(engine).get_columns(_TABLE)}
        with engine.connect() as connection:
            assert dict(connection.execute(text(f"SELECT * FROM {_TABLE}")).mappings().one()) == before
        run_upgrade(migration_url, "head", bootstrap_legacy=False)
        assert run_upgrade(migration_url, "head", bootstrap_legacy=False).current_revision == _current_head(
            migration_url
        )
        assert check_schema_drift(migration_url) == ()
    finally:
        engine.dispose()
