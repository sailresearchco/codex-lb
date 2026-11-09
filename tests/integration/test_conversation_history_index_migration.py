"""Conversation index replacement preserves logs and supports rollback."""

from datetime import datetime

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, inspect, text

from app.db.migrate import _build_alembic_config, check_schema_drift, run_upgrade
from app.db.migration_url import to_sync_database_url
from tests.integration.test_affinity_identity_migration import _current_head
from tests.integration.test_affinity_identity_migration import (
    migration_url as migration_url,
)

pytestmark = pytest.mark.integration
_PARENT = "20260918_000000_merge_scim_and_overflow_heads"
_REVISION = "20261109_191616_conversation_history_index"
_OLD_INDEX = "idx_logs_conversation_id"
_NEW_INDEX = "idx_logs_conversation_deleted"


def _indexes(engine: Engine) -> dict[str | None, list[str] | list[str | None]]:
    return {index["name"]: index["column_names"] for index in inspect(engine).get_indexes("request_logs")}


def _logs(engine: Engine) -> list[tuple]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text("SELECT * FROM request_logs ORDER BY id"))]


def test_conversation_index_replacement_preserves_history_and_round_trips(migration_url: str) -> None:
    url = migration_url
    run_upgrade(url, _PARENT, bootstrap_legacy=False)
    engine = create_engine(to_sync_database_url(url))
    try:
        with engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO request_logs (request_id, model, status, conversation_id, deleted_at)
                VALUES (:request_id, 'synthetic-model', 'success', :conversation_id, :deleted_at)
                """),
                [
                    {"request_id": "live", "conversation_id": "same-conversation", "deleted_at": None},
                    {
                        "request_id": "deleted",
                        "conversation_id": "same-conversation",
                        "deleted_at": datetime(2026, 11, 1),
                    },
                    {"request_id": "untracked", "conversation_id": None, "deleted_at": None},
                ],
            )
        before = _logs(engine)
        original_indexes = _indexes(engine)
        assert original_indexes[_OLD_INDEX] == ["conversation_id"]
        assert _NEW_INDEX not in original_indexes

        assert run_upgrade(url, _REVISION, bootstrap_legacy=False).current_revision == _REVISION
        expected_indexes = {name: columns for name, columns in original_indexes.items() if name != _OLD_INDEX}
        expected_indexes[_NEW_INDEX] = ["conversation_id", "deleted_at"]
        assert _indexes(engine) == expected_indexes
        assert _logs(engine) == before

        command.downgrade(_build_alembic_config(url), _PARENT)
        assert _indexes(engine) == original_indexes
        assert _logs(engine) == before

        # Retry the migration after a build completed but before its revision
        # was recorded. The replacement must not accumulate duplicate indexes.
        with engine.begin() as connection:
            connection.execute(text(f"CREATE INDEX {_NEW_INDEX} ON request_logs (conversation_id, deleted_at)"))
        assert run_upgrade(url, _REVISION, bootstrap_legacy=False).current_revision == _REVISION
        assert _indexes(engine) == expected_indexes
        assert _logs(engine) == before
        assert run_upgrade(url, "head", bootstrap_legacy=False).current_revision == _current_head(url)
        assert check_schema_drift(url) == ()
    finally:
        engine.dispose()


def test_conversation_index_replacement_repairs_invalid_postgresql_build(migration_url: str) -> None:
    url = migration_url
    if not url.startswith("postgresql"):
        pytest.skip("PostgreSQL-only interrupted concurrent index build")
    run_upgrade(url, _PARENT, bootstrap_legacy=False)
    engine = create_engine(to_sync_database_url(url))
    try:
        with engine.begin() as connection:
            connection.execute(text(f"CREATE INDEX {_NEW_INDEX} ON request_logs (conversation_id)"))
            connection.execute(
                text("UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass(:name)"),
                {"name": _NEW_INDEX},
            )
        run_upgrade(url, _REVISION, bootstrap_legacy=False)
        indexes = _indexes(engine)
        assert indexes[_NEW_INDEX] == ["conversation_id", "deleted_at"]
        assert _OLD_INDEX not in indexes
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
                {"name": _NEW_INDEX},
            ).scalar_one()
    finally:
        engine.dispose()
