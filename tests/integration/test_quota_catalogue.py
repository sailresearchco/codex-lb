from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

import pytest
from aiosqlite import Connection as AsyncSQLiteConnection
from sqlalchemy import insert, select

from app.db.models import AdditionalUsageHistory
from app.db.session import SessionLocal
from app.modules.usage.additional_quota_keys import canonicalize_additional_quota_key
from app.modules.usage.repository import AdditionalUsageRepository
from tests.integration.test_reports_api import _make_account

pytestmark = pytest.mark.integration
NOW = datetime(2026, 5, 20, 12)


async def _seed(repeats: int = 2) -> None:
    async with SessionLocal() as session:
        session.add_all([_make_account(account, f"{account}@example.com") for account in ("a", "aa", "b")])
        await session.flush()
        await session.execute(
            insert(AdditionalUsageHistory),
            [
                {
                    "account_id": account,
                    "quota_key": quota,
                    "limit_name": limit,
                    "metered_feature": feature,
                    "window": "primary",
                    "used_percent": 10,
                    "recorded_at": NOW - timedelta(days=age),
                }
                for _ in range(repeats)
                for account, quota, limit, feature, age in (
                    ("a", "codex_spark", "GPT-5.3-Codex-Spark", "codex_bengalfox", 0),
                    ("a", "", "GPT-5.3-Codex-Spark", "codex_bengalfox", 2),
                    ("a", "", "GPT-5.3-Codex-Spark", "other_feature", 2),
                    ("a", "", "", "", 0),
                    ("aa", "legacy", "Legacy", "other", 2),
                    ("b", "legacy", "legacy", "Other", 0),
                    ("b", "other", "Other", "other", 0),
                )
            ],
        )
        await session.commit()


@pytest.mark.parametrize("account_ids", [None, [], ["a"], ["aa", "b"], ["missing"]])
@pytest.mark.parametrize("since", [None, NOW - timedelta(days=1)])
async def test_quota_catalogue_matches_distinct_labels(db_setup, account_ids, since):
    await _seed()
    async with SessionLocal() as session:
        stmt = select(
            AdditionalUsageHistory.quota_key,
            AdditionalUsageHistory.limit_name,
            AdditionalUsageHistory.metered_feature,
        ).distinct()
        if account_ids is not None:
            stmt = stmt.where(AdditionalUsageHistory.account_id.in_(account_ids))
        if since is not None:
            stmt = stmt.where(AdditionalUsageHistory.recorded_at >= since)
        expected = sorted(
            {
                key
                for quota, limit, feature in (await session.execute(stmt)).all()
                if (
                    key := canonicalize_additional_quota_key(quota_key=quota, limit_name=limit, metered_feature=feature)
                )
                is not None
            }
        )
        assert (
            await AdditionalUsageRepository(session).list_quota_keys(account_ids=account_ids, since=since) == expected
        )


async def test_quota_catalogue_work_scales_with_labels_not_history(db_setup):
    async with SessionLocal() as session:
        if session.get_bind().dialect.name != "sqlite":
            pytest.skip("SQLite virtual-machine work regression")
    await _seed(repeats=2000)
    async with SessionLocal() as session:
        connection = await session.connection()
        driver = cast(AsyncSQLiteConnection, (await connection.get_raw_connection()).driver_connection)
        operations = 0

        def progress() -> int:
            nonlocal operations
            operations += 100
            return int(operations > 10_000)

        await driver.set_progress_handler(progress, 100)
        try:
            repo = AdditionalUsageRepository(session)
            assert await repo.list_quota_keys() == ["codex_spark", "legacy", "other"]
            assert await repo.list_quota_keys(account_ids=["a"]) == ["codex_spark"]
            assert await repo.list_quota_keys(account_ids=["aa", "b"]) == ["legacy", "other"]
        finally:
            await driver.set_progress_handler(lambda: 0, 0)
