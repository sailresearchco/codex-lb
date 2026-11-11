"""Add background-folded report speed histograms without a historical scan."""

import sqlalchemy as sa
from alembic import op

revision = "20261110_053002_add_report_speed_histograms"
down_revision = "20261110_053001_add_conversation_summaries"
branch_labels = None
depends_on = None

_TABLE = "request_report_speed_histograms"
_STATE = "account_usage_rollup_state"
_COLUMN = "speed_folded_through"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("bucket_epoch", sa.BigInteger(), primary_key=True),
            sa.Column("account_id", sa.String(), primary_key=True),
            sa.Column("api_key_id", sa.String(), primary_key=True),
            sa.Column("model", sa.String(), primary_key=True),
            sa.Column("useragent_group", sa.String(), primary_key=True),
            sa.Column("metric", sa.String(), primary_key=True),
            sa.Column("bin_index", sa.Integer(), primary_key=True),
            sa.Column("sample_count", sa.BigInteger(), nullable=False),
        )
    if _COLUMN not in {column["name"] for column in inspector.get_columns(_STATE)}:
        op.add_column(
            _STATE,
            sa.Column(_COLUMN, sa.DateTime(), nullable=False, server_default=sa.text("'1970-01-01 00:00:00'")),
        )
    if "speed_history_from" not in {column["name"] for column in inspector.get_columns(_STATE)}:
        op.add_column(_STATE, sa.Column("speed_history_from", sa.DateTime(), nullable=True))
    if "request_logs_pruned_through" not in {column["name"] for column in inspector.get_columns(_STATE)}:
        op.add_column(_STATE, sa.Column("request_logs_pruned_through", sa.DateTime(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "request_logs_pruned_through" in {column["name"] for column in inspector.get_columns(_STATE)}:
        with op.batch_alter_table(_STATE) as batch_op:
            batch_op.drop_column("request_logs_pruned_through")
    if "speed_history_from" in {column["name"] for column in inspector.get_columns(_STATE)}:
        with op.batch_alter_table(_STATE) as batch_op:
            batch_op.drop_column("speed_history_from")
    if _COLUMN in {column["name"] for column in inspector.get_columns(_STATE)}:
        with op.batch_alter_table(_STATE) as batch_op:
            batch_op.drop_column(_COLUMN)
    if inspector.has_table(_TABLE):
        op.drop_table(_TABLE)
