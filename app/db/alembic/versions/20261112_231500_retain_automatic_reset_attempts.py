"""Retain automatic reset attempts through their coupon expiry."""

import sqlalchemy as sa
from alembic import op

revision = "20261112_231500_retain_automatic_reset_attempts"
down_revision = "20261112_002527_api_key_usage_countable_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("reset_credit_redeem_requests")}
    if "expires_at" not in columns:
        op.add_column(
            "reset_credit_redeem_requests",
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("reset_credit_redeem_requests") as batch_op:
        batch_op.drop_column("expires_at")
