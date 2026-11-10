"""Add resumable retained-history conversation projections."""

import sqlalchemy as sa
from alembic import op

revision = "20261110_053001_add_conversation_summaries"
down_revision = "20261110_051702_add_upgrade_repair_target"
branch_labels = None
depends_on = None


def _create_table(name: str, *columns: sa.Column) -> None:
    # PostgreSQL concurrent index creation commits preceding DDL. A failed
    # build must be resumable without recreating populated projections.
    if not sa.inspect(op.get_bind()).has_table(name):
        op.create_table(name, *columns)


def upgrade() -> None:
    state_columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("account_usage_rollup_state")}
    if "conversation_reset_after_id" not in state_columns:
        op.add_column(
            "account_usage_rollup_state", sa.Column("conversation_reset_after_id", sa.BigInteger(), nullable=True)
        )
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("request_logs")}
    if "conversation_summary_included" not in columns:
        op.add_column(
            "request_logs",
            sa.Column("conversation_summary_included", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    _create_table(
        "request_conversation_summaries",
        sa.Column("conversation_id", sa.String(), primary_key=True),
        sa.Column("first_requested_at", sa.DateTime(), nullable=False),
        sa.Column("last_requested_at", sa.DateTime(), nullable=False),
        sa.Column("request_count", sa.BigInteger(), nullable=False),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cached_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cached_known_count", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
    )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("request_conversation_summaries")}
    if "idx_conversation_summary_latest" not in indexes:
        op.create_index(
            "idx_conversation_summary_latest",
            "request_conversation_summaries",
            [sa.text("last_requested_at DESC"), "conversation_id"],
        )
    _create_table(
        "request_conversation_facets",
        sa.Column("conversation_id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("value", sa.String(), primary_key=True),
        sa.Column("request_count", sa.BigInteger(), nullable=False),
        sa.Column("last_requested_at", sa.DateTime(), nullable=False),
    )
    dialect = op.get_bind().dialect.name
    _create_table(
        "request_conversation_model_summaries",
        sa.Column("conversation_id", sa.String(), primary_key=True),
        sa.Column("model", sa.String(), primary_key=True),
        sa.Column("reasoning_effort", sa.String(), primary_key=True),
        *(
            sa.Column(name, sa.BigInteger(), nullable=False)
            for name in (
                "request_count",
                "total_elapsed_ms",
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "cached_known_count",
            )
        ),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("last_requested_at", sa.DateTime(), nullable=False),
    )
    false_value, true_value = ("FALSE", "TRUE") if dialect == "postgresql" else ("0", "1")
    definitions = (
        ("idx_logs_conversation_summary_pending", "id", f"conversation_summary_included IS {false_value}"),
        (
            "idx_logs_conversation_summary_time",
            "conversation_id, requested_at",
            f"conversation_summary_included IS {true_value} AND deleted_at IS NULL "
            "AND request_kind NOT IN ('warmup', 'limit_warmup') "
            "AND conversation_id IS NOT NULL AND conversation_id != ''",
        ),
    )
    if dialect == "postgresql":
        with op.get_context().autocommit_block():
            for name, columns, predicate in definitions:
                invalid = (
                    op.get_bind()
                    .execute(
                        sa.text("SELECT 1 FROM pg_index WHERE indexrelid = to_regclass(:name) AND NOT indisvalid"),
                        {"name": name},
                    )
                    .scalar()
                )
                if invalid:
                    op.execute(sa.text(f"DROP INDEX CONCURRENTLY {name}"))
                op.execute(
                    sa.text(
                        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON request_logs ({columns}) WHERE {predicate}"
                    )
                )
    else:
        for name, columns, predicate in definitions:
            op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {name} ON request_logs ({columns}) WHERE {predicate}"))


def downgrade() -> None:
    op.drop_index("idx_logs_conversation_summary_time", table_name="request_logs")
    op.drop_index("idx_logs_conversation_summary_pending", table_name="request_logs")
    op.drop_table("request_conversation_facets")
    op.drop_table("request_conversation_model_summaries")
    op.drop_table("request_conversation_summaries")
    with op.batch_alter_table("request_logs") as batch:
        batch.drop_column("conversation_summary_included")
    with op.batch_alter_table("account_usage_rollup_state") as batch:
        batch.drop_column("conversation_reset_after_id")
