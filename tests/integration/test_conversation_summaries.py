from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from time import monotonic
from typing import cast

import pytest
from aiosqlite import Connection as AsyncSQLiteConnection
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.modules.request_logs.conversation_summary as summaries
import app.modules.request_logs.cost_backfill as cost_backfill
import app.modules.request_logs.repository as logs_module
from app.core.retention.job import _prune_request_logs
from app.core.utils.time import utcnow
from app.db.dashboard_reads import get_dashboard_read_session
from app.db.models import Account, AccountUsageRollupState, RequestConversationSummary, RequestLog
from app.db.session import SessionLocal, engine
from app.modules.accounts.deletion import run_account_deletion_pass
from app.modules.accounts.repository import AccountsRepository
from app.modules.request_logs.repository import RequestLogsRepository
from tests.integration.test_request_usage_time_rollup import _make_account

pytestmark = pytest.mark.integration


async def _finish_backfill():
    while await summaries.backfill_conversation_summaries():
        pass


async def _append(
    request_id: str,
    *,
    account_id: str | None = None,
    model: str = "model",
    cost_usd: float | None = 1.0,
    requested_at: datetime | None = None,
    reasoning_effort: str | None = None,
    useragent_group: str | None = None,
) -> RequestLog:
    async with SessionLocal() as session:
        return await RequestLogsRepository(session).add_log(
            account_id=account_id,
            request_id=request_id,
            model=model,
            input_tokens=10,
            output_tokens=2,
            latency_ms=3,
            status="success",
            error_code=None,
            conversation_id="thread",
            cost_usd=cost_usd,
            requested_at=requested_at,
            reasoning_effort=reasoning_effort,
            useragent_group=useragent_group,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_routes_wait_for_atomic_resumable_backfill_and_detect_old_writer(
    async_client, db_setup, monkeypatch, failure
):
    now = utcnow().replace(microsecond=0)
    async with SessionLocal() as session:
        await session.execute(
            insert(RequestLog),
            [
                dict(
                    request_id=f"old-{index}",
                    model="model",
                    conversation_id="thread",
                    status="success",
                    requested_at=now - timedelta(days=60 if index == 0 else 0),
                    input_tokens=10,
                    output_tokens=2,
                    reasoning_effort=None if index % 2 else "",
                    latency_ms=3,
                    cost_usd=1.0,
                )
                for index in range(5)
            ],
        )
        await session.commit()
    for path in ("/api/conversations", "/api/conversations/thread"):
        response = await async_client.get(path)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "conversation_summaries_building"
    assert await summaries.backfill_conversation_summaries(limit=2) == 2
    original = summaries.add_contributions

    async def interrupted(session, contributions):
        await original(session, contributions)
        raise failure("interrupted after projection writes")

    monkeypatch.setattr(summaries, "add_contributions", interrupted)
    with pytest.raises(failure):
        await summaries.backfill_conversation_summaries(limit=2)
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).where(RequestLog.conversation_summary_included.is_(True))) == 2
        assert await session.scalar(select(RequestConversationSummary.request_count)) == 2
    monkeypatch.setattr(summaries, "add_contributions", original)
    await _finish_backfill()
    await _append("late", requested_at=now - timedelta(days=90), reasoning_effort="")
    result = await async_client.get("/api/conversations")
    assert result.status_code == 200
    row = result.json()["conversations"][0]
    assert row["requestCount"] == 6
    assert row["firstRequest"] == (now - timedelta(days=90)).isoformat() + "Z"
    detail = (await async_client.get("/api/conversations/thread")).json()
    assert {row["modelEffort"]["reasoningEffort"] for row in detail["modelStats"]} == {None, ""}
    assert sum(row["reqs"] for row in detail["modelStats"]) == 6
    async with SessionLocal() as session:
        await session.execute(
            insert(RequestLog).values(
                request_id="old-writer", model="model", status="success", conversation_id="thread", requested_at=now
            )
        )
        await session.commit()
    assert (await async_client.get("/api/conversations")).status_code == 503
    await _finish_backfill()
    assert (await async_client.get("/api/conversations")).json()["conversations"][0]["requestCount"] == 7


@pytest.mark.asyncio
async def test_model_and_missing_cost_corrections_update_list_and_details(async_client, db_setup, monkeypatch):
    await _append("rewrite", model="original")
    monkeypatch.setattr(logs_module, "calculated_cost_from_log", lambda log: 4.0)
    async with SessionLocal() as session:
        assert await RequestLogsRepository(session).update_model_for_request("rewrite", "replacement") == 1
    listing = (await async_client.get("/api/conversations")).json()["conversations"][0]
    assert listing["representativeModel"] == "replacement"
    assert listing["totalCostUsd"] == 4.0
    detail = (await async_client.get("/api/conversations/thread")).json()
    assert detail["modelStats"][0]["modelEffort"]["model"] == "replacement"
    monkeypatch.setattr(logs_module, "calculated_cost_from_log", lambda log: None)
    await _append("missing-cost", cost_usd=None)
    monkeypatch.setattr(cost_backfill, "calculated_cost_from_log", lambda log: 7.0)
    async with SessionLocal() as session:
        result = await cost_backfill.backfill_missing_costs(session)
        assert result.updated == 1
    assert (await async_client.get("/api/conversations")).json()["conversations"][0]["totalCostUsd"] == 11.0
    assert (
        sum(row["totalCostUsd"] for row in (await async_client.get("/api/conversations/thread")).json()["modelStats"])
        == 11.0
    )


@pytest.mark.asyncio
async def test_account_merge_soft_delete_and_chunked_hard_delete_preserve_projection(
    async_client, db_setup, monkeypatch
):
    monkeypatch.setattr(logs_module, "_COUNT_CACHE_TTL_SECONDS", 30.0)
    now = utcnow().replace(microsecond=0)
    async with SessionLocal() as session:
        for name in ("a", "b", "c"):
            await AccountsRepository(session).upsert(_make_account(name, f"{name}@example.invalid"))
    for account in ("a", "b", "c"):
        await _append(account, account_id=account, requested_at=now, useragent_group=account)
    assert (await async_client.get("/api/conversations")).json()["total"] == 1
    async with SessionLocal() as session:
        await session.execute(
            update(Account)
            .where(Account.id.in_(["a", "b"]))
            .values(chatgpt_account_id="shared", email="a@example.invalid")
        )
        await session.commit()
        await AccountsRepository(session).upsert(
            _make_account("a", "a@example.invalid", chatgpt_account_id="shared"),
            merge_by_email=False,
            merge_by_chatgpt_identity=True,
        )
    detail = (await async_client.get("/api/conversations/thread")).json()
    assert detail["accountCount"] == 2
    assert sum(row["reqs"] for row in detail["modelStats"]) == 3
    async with SessionLocal() as session:
        assert await AccountsRepository(session).delete("a")
    detail = (await async_client.get("/api/conversations/thread")).json()
    assert detail["accountCount"] == 1
    assert detail["dominantUseragentGroup"] == "c"
    assert sum(row["reqs"] for row in detail["modelStats"]) == 1
    async with SessionLocal() as session:
        assert await AccountsRepository(session).begin_delete("c", delete_history=True)
    await run_account_deletion_pass(batch_size=1)
    assert (await async_client.get("/api/conversations")).json()["total"] == 0
    assert (await async_client.get("/api/conversations/thread")).status_code == 404


@pytest.mark.asyncio
async def test_retention_repairs_extrema_and_reused_rowid_is_included_once(async_client, db_setup):
    now = utcnow().replace(microsecond=0)
    for index, days in enumerate((40, 40, 2, 0)):
        await _append(f"retained-{index}", requested_at=now - timedelta(days=days))
    await _finish_backfill()
    async with SessionLocal() as session:
        await session.execute(
            update(AccountUsageRollupState).values(
                folded_through=now,
                hourly_folded_through=now,
                conversation_folded_through=now,
                reports_folded_through=now,
                speed_folded_through=now,
            )
        )
        await session.commit()
    assert await _prune_request_logs(now - timedelta(days=1), now=now) == 3
    result = (await async_client.get("/api/conversations")).json()["conversations"][0]
    assert result["requestCount"] == 1
    assert result["totalTokens"] == 12
    assert result["firstRequest"] == now.isoformat() + "Z"
    # A raw row ID may be reused after deletion; inclusion belongs to the row,
    # never to a remembered maximum ID. Simulate an old-version insertion.
    async with SessionLocal() as session:
        await session.execute(
            insert(RequestLog).values(
                id=1, request_id="reused", model="model", status="success", conversation_id="thread", requested_at=now
            )
        )
        await session.commit()
    assert (await async_client.get("/api/conversations")).status_code == 503
    await _finish_backfill()
    assert (await async_client.get("/api/conversations")).json()["conversations"][0]["requestCount"] == 2


@pytest.mark.asyncio
async def test_dense_history_list_detail_search_and_deep_page_fit_sqlite_vm_budget(
    async_client, app_instance, db_setup
):
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite VM budget regression")
    now = utcnow().replace(microsecond=0)
    async with SessionLocal() as session:
        await session.execute(
            insert(RequestLog),
            [
                dict(
                    request_id=f"dense-{index}",
                    model="model",
                    status="success",
                    input_tokens=10,
                    output_tokens=2,
                    conversation_id="dense" if index < 20_000 else f"thread-{index:05}",
                    requested_at=now,
                    useragent_group="agent%_" if index % 2 else "other",
                    latency_ms=3,
                )
                for index in range(21_000)
            ],
        )
        await session.commit()
    await _finish_backfill()
    steps = []

    async def bounded_session():
        async with SessionLocal() as session:
            connection = await session.connection()
            driver = cast(AsyncSQLiteConnection, (await connection.get_raw_connection()).driver_connection)
            count = 0

            def progress():
                nonlocal count
                count += 100
                return int(count > 60_000)

            await driver.set_progress_handler(progress, 100)
            try:
                yield session
            finally:
                steps.append(count)
                await driver.set_progress_handler(lambda: 0, 0)

    app_instance.dependency_overrides[get_dashboard_read_session] = bounded_session
    try:
        first = await async_client.get("/api/conversations", params={"limit": 1})
        deep = await async_client.get("/api/conversations", params={"limit": 1, "offset": 1000})
        match = await async_client.get("/api/conversations", params={"search": "%_", "limit": 1})
        missing = await async_client.get("/api/conversations", params={"search": "absent%_"})
        detail = await async_client.get("/api/conversations/dense")
        assert all(response.status_code == 200 for response in (first, deep, match, missing, detail))
        assert first.json()["total"] == 1001
        assert first.json()["conversations"][0]["requestCount"] == 20_000
        assert deep.json()["conversations"][0]["conversationId"] == "thread-20999"
        assert match.json()["total"] == 501
        assert missing.json()["total"] == 0
        assert detail.json()["modelStats"][0]["reqs"] == 20_000
        assert max(steps) <= 60_000
    finally:
        app_instance.dependency_overrides.pop(get_dashboard_read_session, None)


@pytest.mark.asyncio
async def test_sparse_latest_facet_removal_measures_bounded_index_suffix(async_client, db_setup):
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite index and VM budget regression")
    now = utcnow().replace(microsecond=0)
    async with SessionLocal() as session:
        await AccountsRepository(session).upsert(_make_account("remove-latest", "remove@example.invalid"))
        rows = [
            dict(
                request_id=f"suffix-{index}",
                model="common",
                status="success",
                conversation_id="suffix",
                requested_at=now - timedelta(seconds=20_001 - index),
                useragent_group="common",
            )
            for index in range(20_000)
        ]
        rows.extend(
            [
                dict(
                    request_id="rare-old",
                    model="rare",
                    status="success",
                    conversation_id="suffix",
                    requested_at=now - timedelta(days=2),
                    useragent_group="rare",
                ),
                dict(
                    request_id="rare-latest",
                    model="rare",
                    status="success",
                    conversation_id="suffix",
                    requested_at=now,
                    useragent_group="rare",
                    account_id="remove-latest",
                ),
            ]
        )
        await session.execute(insert(RequestLog), rows)
        await session.commit()
    await _finish_backfill()
    async with engine.connect() as connection, AsyncSession(bind=connection) as session:
        probe = (
            select(RequestLog.requested_at)
            .where(*summaries.eligible_conditions(), RequestLog.conversation_id == "suffix")
            .order_by(RequestLog.requested_at.desc())
            .limit(1)
        )
        compiled = str(probe.compile(dialect=session.get_bind().dialect, compile_kwargs={"literal_binds": True}))
        plan = (await session.execute(text("EXPLAIN QUERY PLAN " + compiled))).all()
        assert any("idx_logs_conversation_summary_time" in row[-1] for row in plan)
        driver = cast(
            AsyncSQLiteConnection, (await (await session.connection()).get_raw_connection()).driver_connection
        )
        steps = 0

        def progress():
            nonlocal steps
            steps += 100
            return int(steps > 1_000_000)

        await driver.set_progress_handler(progress, 100)
        started = monotonic()
        try:
            assert await AccountsRepository(session).delete("remove-latest", delete_history=True)
        finally:
            elapsed = monotonic() - started
            await driver.set_progress_handler(lambda: 0, 0)
        # This records the remaining write-path tradeoff: sparse facet
        # extrema may traverse the indexed suffix, but never fetch raw
        # history into Python or perform that work on a dashboard read.
        print(f"sparse-facet-repair rows=20002 vm_steps={steps} transaction_seconds={elapsed:.6f}")
        assert steps <= 1_000_000
    detail = (await async_client.get("/api/conversations/suffix")).json()
    assert sum(row["reqs"] for row in detail["modelStats"]) == 20_001
    rare = next(row for row in detail["modelStats"] if row["modelEffort"]["model"] == "rare")
    assert rare["reqs"] == 1
