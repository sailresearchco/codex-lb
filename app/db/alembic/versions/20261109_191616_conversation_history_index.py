"""Bound conversation history reads by conversation ID and deletion state.

The composite index retains the conversation-only prefix while letting page
summaries and facets read only the selected conversations' live history.
Build each replacement before dropping its predecessor. PostgreSQL builds
concurrently so request-log inserts continue during the migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261109_191616_conversation_history_index"
down_revision = "20260918_000000_merge_scim_and_overflow_heads"
branch_labels = None
depends_on = None

_OLD_INDEX = "idx_logs_conversation_id"
_NEW_INDEX = "idx_logs_conversation_deleted"


def _replace_index(create_name: str, columns: str, drop_name: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            # An interrupted concurrent build may leave an unusable index
            # that CREATE INDEX IF NOT EXISTS would otherwise accept.
            invalid = bind.execute(
                sa.text("SELECT 1 FROM pg_index WHERE indexrelid = to_regclass(:name) AND NOT indisvalid"),
                {"name": create_name},
            ).scalar()
            if invalid:
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {create_name}"))
            op.execute(sa.text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {create_name} ON request_logs ({columns})"))
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {drop_name}"))
        return

    op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {create_name} ON request_logs ({columns})"))
    op.drop_index(drop_name, table_name="request_logs", if_exists=True)


def upgrade() -> None:
    _replace_index(_NEW_INDEX, "conversation_id, deleted_at", _OLD_INDEX)


def downgrade() -> None:
    _replace_index(_OLD_INDEX, "conversation_id", _NEW_INDEX)
