from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

import pytest
from aiosqlite import Connection as AsyncSQLiteConnection
from sqlalchemy import delete, insert, select

from app.db.models import AccountUsageRollupState, RequestLog
from app.db.session import SessionLocal
from app.modules.accounts.repository import AccountsRepository
from app.modules.accounts.usage_time_rollup import FOLD_LAG, run_hourly_fold_pass
from app.modules.accounts.usage_time_rollup_read import ceil_to_grid
from app.modules.api_keys.repository import ApiKeysRepository
from tests.integration.test_reports_api import _make_account

pytestmark = pytest.mark.integration
NOW = datetime(2026, 5, 20, 12, 37, 0, 123456)


async def _fold(target: datetime) -> None:
    await run_hourly_fold_pass(now=target + FOLD_LAG)
    async with SessionLocal() as session:
        assert await session.scalar(select(AccountUsageRollupState.hourly_folded_through)) == target


async def _seed(async_client, now: datetime) -> str:
    response = await async_client.post("/api/api-keys/", json={"name": "rollup-usage"})
    assert response.status_code == 200
    key_id = response.json()["id"]
    since = now - timedelta(days=7)
    folded_end = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    async with SessionLocal() as session:
        session.add_all(
            [_make_account(account, f"account-{i}@example.com") for i, account in enumerate(("live", "\x1fescaped"))]
        )
        await session.flush()
        # Both window edges, partial leading/trailing hours, and the live-tail
        # boundary participate. A sentinel-prefixed id must not become NULL.
        for i, at in enumerate(
            (
                since,
                since + timedelta(microseconds=1),
                ceil_to_grid(since, 3600),
                since + timedelta(days=3),
                folded_end - timedelta(microseconds=1),
                folded_end,
                now - timedelta(microseconds=1),
            )
        ):
            session.add(
                RequestLog(
                    request_id=f"included-{i}",
                    requested_at=at,
                    api_key_id=key_id,
                    account_id=("live", "\x1fescaped", None)[i % 3],
                    deleted_at=now if i in (3, 4) else None,
                    model=f"model-{i % 2}",
                    request_kind="normal",
                    status=("success", "error", "cancelled")[i % 3],
                    input_tokens=10,
                    output_tokens=None if i % 2 else 0,
                    reasoning_tokens=3,
                    cached_input_tokens=20 if i == 0 else -1,
                    cost_usd=(i + 1) * 0.01,
                )
            )
        for i, (at, request_kind, row_key) in enumerate(
            (
                (since - timedelta(microseconds=1), "normal", key_id),
                (now, "normal", key_id),
                (since - timedelta(days=90), "normal", key_id),
                (since + timedelta(days=1), "warmup", key_id),
                (now - timedelta(minutes=1), "limit_warmup", key_id),
                (since + timedelta(days=2), "normal", "other-key"),
            )
        ):
            session.add(
                RequestLog(
                    request_id=f"excluded-{i}",
                    requested_at=at,
                    api_key_id=row_key,
                    model="excluded",
                    request_kind=request_kind,
                    status="success",
                    input_tokens=1000,
                    cost_usd=10,
                )
            )
        await session.commit()
    return key_id


@pytest.mark.parametrize("aligned", [False, True])
async def test_usage_endpoint_preserves_totals_across_fold_states_and_retention(async_client, monkeypatch, aligned):
    now = NOW.replace(minute=0, second=0, microsecond=0) if aligned else NOW
    monkeypatch.setattr("app.modules.api_keys.service.utcnow", lambda: now)
    key_id = await _seed(async_client, now)
    async with SessionLocal() as session:
        await session.execute(delete(AccountUsageRollupState))
        await session.commit()

    async def read():
        response = await async_client.get(f"/api/api-keys/{key_id}/usage-7d")
        assert response.status_code == 200
        payload = response.json()
        costs = payload["accountCosts"]
        assert [row["costUsd"] for row in costs] == sorted((row["costUsd"] for row in costs), reverse=True)
        # Cost ties have no account-order contract.
        costs.sort(key=lambda row: (-row["costUsd"], row["isDeleted"], row["accountId"] or ""))
        return payload

    baseline = await read()  # Missing watermark: exact finite raw window.
    assert baseline["totalRequests"] == 7
    assert baseline["totalTokens"] == 79  # Zero output does not fall back to reasoning.
    assert baseline["cachedInputTokens"] == 14  # Clamp once after summing, not per row.
    assert baseline["totalCostUsd"] == 0.28
    assert baseline["accountCosts"] == [
        {"accountId": None, "email": None, "costUsd": 0.09, "isDeleted": False},
        {"accountId": None, "email": None, "costUsd": 0.09, "isDeleted": True},
        {"accountId": "live", "email": "account-0@example.com", "costUsd": 0.08, "isDeleted": False},
        {"accountId": "\x1fescaped", "email": "account-1@example.com", "costUsd": 0.02, "isDeleted": False},
    ]

    async with SessionLocal() as session:
        session.add(AccountUsageRollupState(id=1, folded_through=datetime(1970, 1, 1)))
        await session.commit()
    assert await read() == baseline  # Epoch state must ignore uninitialized rollups too.

    since = now - timedelta(days=7)
    for watermark in (
        since.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1),
        since.replace(minute=0, second=0, microsecond=0) + timedelta(days=3),
        now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2),
    ):
        await _fold(watermark)
        assert await read() == baseline

    async with SessionLocal() as session:
        await session.execute(
            delete(RequestLog).where(
                RequestLog.requested_at >= ceil_to_grid(since, 3600),
                RequestLog.requested_at < watermark,
            )
        )
        await session.commit()
    assert await read() == baseline


@pytest.mark.parametrize("delete_history", [False, True])
async def test_usage_endpoint_preserves_account_lifecycle_after_fold(async_client, monkeypatch, delete_history):
    monkeypatch.setattr("app.modules.api_keys.service.utcnow", lambda: NOW)
    key_id = await _seed(async_client, NOW)
    watermark = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    await _fold(watermark)
    async with SessionLocal() as session:
        assert await AccountsRepository(session).delete("live", delete_history=delete_history)
    response = await async_client.get(f"/api/api-keys/{key_id}/usage-7d")
    assert response.status_code == 200
    payload = response.json()
    assert payload["totalRequests"] == (4 if delete_history else 7)
    assert payload["totalCostUsd"] == (0.16 if delete_history else 0.28)
    assert not any(row["accountId"] == "live" for row in payload["accountCosts"])
    deleted = next(row for row in payload["accountCosts"] if row["isDeleted"])
    assert deleted["costUsd"] == (0.05 if delete_history else 0.17)
    unknown = next(row for row in payload["accountCosts"] if row["accountId"] is None and not row["isDeleted"])
    assert unknown["costUsd"] == 0.09


@pytest.mark.parametrize("folded", [False, True])
async def test_usage_read_seeks_past_irrelevant_raw_history(db_setup, folded):
    since = NOW - timedelta(days=7)
    watermark = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    async with SessionLocal() as session:
        if session.get_bind().dialect.name != "sqlite":
            pytest.skip("SQLite virtual-machine work regression")
        await session.execute(
            insert(RequestLog),
            [
                {
                    "request_id": f"folded-{i}",
                    "requested_at": since + timedelta(days=1 if folded else -1, seconds=i),
                    "api_key_id": "key",
                    "model": "m",
                    "status": "success",
                    "input_tokens": 1,
                    "cost_usd": 0.01,
                }
                for i in range(10_000)
            ],
        )
        await session.commit()
    if folded:
        await _fold(watermark)
    async with SessionLocal() as session:
        connection = await session.connection()
        driver = cast(AsyncSQLiteConnection, (await connection.get_raw_connection()).driver_connection)
        operations = 0

        def progress() -> int:
            nonlocal operations
            operations += 100
            return int(operations > 20_000)

        await driver.set_progress_handler(progress, 100)
        try:
            totals = await ApiKeysRepository(session).usage_7d("key", since, NOW)
            assert totals.total_requests == (10_000 if folded else 0)
            assert totals.total_tokens == (10_000 if folded else 0)
            assert totals.total_cost_usd == (100 if folded else 0)
        finally:
            await driver.set_progress_handler(lambda: 0, 0)
