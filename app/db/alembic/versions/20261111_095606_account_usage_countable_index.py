"""Keep lifetime usage reads off excluded request history."""

import sqlalchemy as sa
from alembic import op

revision = "20261111_095606_account_usage_countable_index"
down_revision = "20261110_053003_add_thread_identity_cache"
branch_labels = None
depends_on = None

_INDEX = "idx_logs_countable_account_time"
_DEFINITION = (
    "ON request_logs (deleted_at, requested_at, account_id, request_id, id, request_kind) "
    "WHERE deleted_at IS NULL AND account_id IS NOT NULL "
    "AND request_kind NOT IN ('warmup', 'limit_warmup')"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            invalid = bind.execute(
                sa.text("SELECT 1 FROM pg_index WHERE indexrelid = to_regclass(:name) AND NOT indisvalid"),
                {"name": _INDEX},
            ).scalar()
            if invalid:
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}"))
            op.execute(sa.text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} {_DEFINITION}"))
        return
    op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {_INDEX} {_DEFINITION}"))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}"))
    else:
        op.drop_index(_INDEX, table_name="request_logs", if_exists=True)
