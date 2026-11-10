"""Add disposable retained-history thread identity caches without backfilling."""

import sqlalchemy as sa
from alembic import op

revision = "20261110_053003_add_thread_identity_cache"
down_revision = "20261110_053002_add_report_speed_histograms"
branch_labels = None
depends_on = None


def _keys():
    return [
        sa.Column("bucket_epoch", sa.BigInteger(), primary_key=True),
        sa.Column("keyed", sa.Integer(), primary_key=True),
        sa.Column("thread_key", sa.String(), primary_key=True),
    ]


def _create_table(name: str, *columns: sa.Column) -> None:
    # Legacy-ledger recovery can replay this revision over populated caches.
    if not sa.inspect(op.get_bind()).has_table(name):
        op.create_table(name, *columns)


def upgrade() -> None:
    _create_table(
        "thread_identity_hours",
        *_keys(),
        *(
            sa.Column(name, sa.BigInteger(), nullable=False)
            for name in ("requests", "cache_input_tokens", "cache_cached_input_tokens")
        ),
        *(
            column
            for prefix in ("first", "last")
            for column in (
                sa.Column(f"{prefix}_at", sa.DateTime(), nullable=False),
                sa.Column(f"{prefix}_id", sa.BigInteger(), nullable=False),
                sa.Column(f"{prefix}_account", sa.String(), nullable=True),
                sa.Column(f"{prefix}_input", sa.BigInteger(), nullable=True),
                sa.Column(f"{prefix}_epoch", sa.BigInteger(), nullable=False),
            )
        ),
    )
    _create_table(
        "thread_identity_account_hours",
        *_keys(),
        sa.Column("account_id", sa.String(), primary_key=True),
        sa.Column("requests", sa.BigInteger(), nullable=False),
    )
    _create_table(
        "thread_identity_pair_hours",
        *_keys(),
        sa.Column("from_account", sa.String(), primary_key=True),
        sa.Column("to_account", sa.String(), primary_key=True),
        sa.Column("turns", sa.BigInteger(), nullable=False),
    )
    _create_table(
        "thread_identity_dirty_hours",
        sa.Column("bucket_epoch", sa.BigInteger(), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("account_usage_rollup_state")}
    if "thread_folded_through" not in columns:
        op.add_column(
            "account_usage_rollup_state",
            sa.Column(
                "thread_folded_through", sa.DateTime(), nullable=False, server_default=sa.text("'1970-01-01 00:00:00'")
            ),
        )


def downgrade() -> None:
    for name in (
        "thread_identity_dirty_hours",
        "thread_identity_pair_hours",
        "thread_identity_account_hours",
        "thread_identity_hours",
    ):
        op.drop_table(name)
    with op.batch_alter_table("account_usage_rollup_state") as batch:
        batch.drop_column("thread_folded_through")
