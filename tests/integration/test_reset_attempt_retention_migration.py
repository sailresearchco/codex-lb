from datetime import datetime

import pytest
from alembic import command
from sqlalchemy import DateTime, create_engine, insert, inspect, text

from app.db.migrate import _build_alembic_config, check_schema_drift, run_upgrade
from app.db.migration_url import to_sync_database_url
from app.db.models import Account
from tests.integration.test_affinity_identity_migration import migration_url as migration_url

pytestmark = pytest.mark.integration
PARENT = "20261112_002527_api_key_usage_countable_index"


def test_reset_attempt_retention_upgrade_downgrade_preserves_pins(migration_url):
    run_upgrade(migration_url, PARENT, bootstrap_legacy=False)
    engine = create_engine(to_sync_database_url(migration_url))
    try:
        with engine.begin() as connection:
            connection.execute(
                insert(Account).values(
                    id="retention-account",
                    email="retention@example.invalid",
                    plan_type="pro",
                    access_token_encrypted=b"fixture",
                    refresh_token_encrypted=b"fixture",
                    id_token_encrypted=b"fixture",
                    last_refresh=datetime(2026, 11, 12),
                )
            )
            connection.execute(
                text(
                    "INSERT INTO reset_credit_redeem_requests (account_id,redeem_request_id,credit_id,created_at) "
                    "VALUES (:account,'manual','coupon',:created)"
                ),
                {"account": "retention-account", "created": datetime(2026, 11, 12)},
            )
            before = connection.execute(
                text("SELECT account_id,redeem_request_id,credit_id,created_at FROM reset_credit_redeem_requests")
            ).all()
        for _ in range(2):
            run_upgrade(migration_url, "head", bootstrap_legacy=False)
            columns = {column["name"]: column for column in inspect(engine).get_columns("reset_credit_redeem_requests")}
            assert columns["expires_at"]["nullable"]
            assert check_schema_drift(migration_url) == ()
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text(
                            "SELECT account_id,redeem_request_id,credit_id,created_at FROM reset_credit_redeem_requests"
                        )
                    ).all()
                    == before
                )
                assert (
                    connection.execute(text("SELECT expires_at FROM reset_credit_redeem_requests")).scalar_one() is None
                )
            command.downgrade(_build_alembic_config(migration_url), PARENT)
            assert "expires_at" not in {
                column["name"] for column in inspect(engine).get_columns("reset_credit_redeem_requests")
            }
        with engine.begin() as connection:
            timestamp_type = DateTime(timezone=True).compile(dialect=engine.dialect)
            connection.execute(text(f"ALTER TABLE reset_credit_redeem_requests ADD COLUMN expires_at {timestamp_type}"))
        run_upgrade(migration_url, "head", bootstrap_legacy=False)
        assert check_schema_drift(migration_url) == ()
    finally:
        engine.dispose()
