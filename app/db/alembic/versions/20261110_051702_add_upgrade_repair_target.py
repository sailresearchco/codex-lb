"""Persist the fixed end of an in-progress hourly rollup repair."""

import sqlalchemy as sa
from alembic import op

revision = "20261110_051702_add_upgrade_repair_target"
down_revision = "20261109_191616_conversation_history_index"
branch_labels = None
depends_on = None

_TABLE = "account_usage_rollup_state"
_COLUMN = "upgrade_repair_through"


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if _COLUMN not in columns:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.DateTime(), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if _COLUMN in columns:
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_column(_COLUMN)
