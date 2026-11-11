from datetime import datetime, timedelta
from typing import cast

import pytest
from aiosqlite import Connection as AsyncSQLiteConnection
from sqlalchemy import delete, insert, select

from app.core.exceptions import DashboardServiceUnavailableError
from app.db import dashboard_reads
from app.db.models import AccountUsageRollupState, RequestLog
from app.db.session import SessionLocal, engine
from app.modules.accounts.repository import AccountsRepository
from app.modules.accounts.usage_rollup import run_fold_pass
from app.modules.reports.repository import ReportsRepository
from app.modules.reports.rollup import fold_next_report_slice
from app.modules.usage.read_coverage import require_bounded_account_history
from tests.integration.test_reports_api import _make_account

pytestmark = pytest.mark.integration
NOW = datetime(2026, 11, 10, 12)
EPOCH = datetime(1970, 1, 1)
PARAMS = {"start_date": "2026-11-04", "end_date": "2026-11-10"}


@pytest.fixture(autouse=True)
def fixed_read_time(monkeypatch):
    for module in ("accounts.repository", "reports.repository", "reports.service"):
        monkeypatch.setattr(f"app.modules.{module}.utcnow", lambda: NOW)


async def _seed(at: datetime, *, count: int = 1, watermark: datetime | None = None) -> None:
    async with SessionLocal() as session:
        session.add(_make_account("coverage-account", "coverage@example.invalid"))
        await session.flush()
        await session.execute(
            insert(RequestLog),
            [
                {
                    "account_id": "coverage-account",
                    "request_id": f"coverage-{i}",
                    "requested_at": at,
                    "model": "coverage-model",
                    "api_key_id": "deleted-key",
                    "status": "success",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cost_usd": 0.25,
                }
                for i in range(count)
            ],
        )
        await session.execute(delete(AccountUsageRollupState))
        if watermark is not None:
            session.add(
                AccountUsageRollupState(
                    id=1, folded_through=watermark, reports_folded_through=watermark, upgrade_repair_from=None
                )
            )
        await session.commit()


async def _fold() -> None:
    await run_fold_pass(now=NOW)
    async with SessionLocal() as session:
        while await fold_next_report_slice(session, NOW - timedelta(hours=2)):
            pass


@pytest.mark.parametrize("path", ["/api/accounts", "/api/reports", "/api/reports/options"])
@pytest.mark.parametrize("watermark", [None, EPOCH, NOW - timedelta(days=2)])
async def test_cold_history_refuses_before_aggregate_work_and_retry_recovers(
    async_client, db_setup, monkeypatch, path, watermark
):
    count = 8000 if watermark is None else 1
    await _seed(NOW - timedelta(days=1), count=count, watermark=watermark)
    operations = 0
    original = dashboard_reads._ReadBudget.progress

    def count_work(budget):
        nonlocal operations
        operations += 1
        return int(operations > 25000 or original(budget))

    # The real connection-owned deadline installs this callback. Unlike an
    # on-connect handler, it is not overwritten by request budget setup.
    if engine.dialect.name == "sqlite":
        monkeypatch.setattr(dashboard_reads, "_PROGRESS_STEPS", 1)
        monkeypatch.setattr(dashboard_reads._ReadBudget, "progress", count_work)
    response = await async_client.get(path, params={} if path == "/api/accounts" else PARAMS)
    assert response.status_code == 503
    expected_code = "usage_history_building" if path == "/api/accounts" else "report_history_building"
    assert response.json()["error"]["code"] == expected_code
    if engine.dialect.name == "sqlite":
        assert 0 < operations <= 25000
        monkeypatch.setattr(dashboard_reads._ReadBudget, "progress", original)

    await _fold()
    response = await async_client.get(path, params={} if path == "/api/accounts" else PARAMS)
    assert response.status_code == 200
    if path == "/api/accounts":
        assert response.json()["accounts"][0]["requestUsage"]["requestCount"] == count
    elif path == "/api/reports":
        assert response.json()["summary"]["totalRequests"] == count
        assert response.json()["summary"]["totalCostUsd"] == count * 0.25
    else:
        assert response.json()["models"] == ["coverage-model"]
        assert "deleted-key" in {key["id"] for key in response.json()["apiKeys"]}


@pytest.mark.parametrize("watermark", [None, EPOCH, NOW - timedelta(days=2)])
@pytest.mark.parametrize("age", [timedelta(hours=3), timedelta(hours=4)])
async def test_sparse_old_frontier_preserves_recent_installation(async_client, db_setup, watermark, age):
    await _seed(NOW - age, watermark=watermark)
    for path in ("/api/accounts", "/api/reports", "/api/reports/options"):
        response = await async_client.get(path, params={} if path == "/api/accounts" else PARAMS)
        assert response.status_code == 200, response.json()


async def test_empty_database_and_future_report_window_remain_available(async_client, db_setup):
    async with SessionLocal() as session:
        await session.execute(delete(AccountUsageRollupState))
        await session.commit()
    for path in ("/api/accounts", "/api/reports", "/api/reports/options"):
        response = await async_client.get(
            path, params={} if path == "/api/accounts" else {"start_date": "2026-11-11", "end_date": "2026-11-12"}
        )
        assert response.status_code == 200, response.json()


async def test_comparison_history_is_guarded_even_when_selected_period_is_empty(async_client, db_setup):
    await _seed(datetime(2026, 10, 28))
    response = await async_client.get("/api/reports", params=PARAMS)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "report_history_building"
    await _fold()
    response = await async_client.get("/api/reports", params=PARAMS)
    assert response.status_code == 200
    assert response.json()["summary"]["totalRequests"] == 0
    assert response.json()["comparison"]["previous"]["totalRequests"] == 1


async def test_folded_historical_window_and_timezone_edges_preserve_exact_results(async_client, db_setup):
    # Asia/Kolkata's day starts inside a UTC hour. The old global watermark
    # covers the selected period; its partial storage hour remains raw.
    at = datetime(2026, 10, 31, 18, 30)
    await _seed(at)
    async with SessionLocal() as session:
        session.add(
            RequestLog(
                account_id="coverage-account",
                request_id="outside-day",
                requested_at=at - timedelta(seconds=1),
                model="coverage-model",
                status="success",
                cost_usd=1,
            )
        )
        await session.commit()
        while await fold_next_report_slice(session, datetime(2026, 11, 2)):
            pass
    response = await async_client.get(
        "/api/reports", params={"start_date": "2026-11-01", "end_date": "2026-11-01", "timezone": "Asia/Kolkata"}
    )
    assert response.status_code == 200
    assert response.json()["summary"]["totalRequests"] == 1
    assert response.json()["summary"]["totalCostUsd"] == 0.25


async def test_comparison_includes_activity_exactly_at_folded_hour_boundary(async_client, db_setup):
    await _seed(datetime(2026, 11, 3))
    await _fold()
    response = await async_client.get("/api/reports", params={"start_date": "2026-11-04", "end_date": "2026-11-04"})
    assert response.status_code == 200
    assert response.json()["comparison"]["canCompare"] is True


@pytest.mark.parametrize("kind", ["accounts", "reports"])
async def test_late_insert_after_coverage_probe_is_not_silently_clamped(db_setup, monkeypatch, kind):
    await _seed(NOW - timedelta(hours=1), watermark=NOW - timedelta(days=1))
    name = "require_bounded_account_history" if kind == "accounts" else "require_bounded_raw_history"
    module = __import__(f"app.modules.{kind}.repository", fromlist=[name])
    original = getattr(module, name)
    inserted = False

    async def insert_after_probe(*args, **kwargs):
        nonlocal inserted
        await original(*args, **kwargs)
        if not inserted:
            inserted = True
            async with SessionLocal() as session:
                session.add(
                    RequestLog(
                        account_id="coverage-account",
                        request_id="late",
                        requested_at=NOW - timedelta(hours=6),
                        model="coverage-model",
                        status="success",
                        cost_usd=0.5,
                    )
                )
                await session.commit()

    monkeypatch.setattr(module, name, insert_after_probe)
    async with SessionLocal() as session:
        if kind == "accounts":
            totals = await AccountsRepository(session).list_request_usage_summary_by_account(["coverage-account"])
            assert totals["coverage-account"].request_count == 2
        else:
            repository = ReportsRepository(session)
            await repository.require_history_coverage([(datetime(2026, 11, 10), datetime(2026, 11, 11))])
            totals = await repository.aggregate_summary(datetime(2026, 11, 10), datetime(2026, 11, 11))
            assert totals.total_requests == 2


async def test_api_key_catalog_cannot_bypass_coverage(db_setup):
    await _seed(NOW - timedelta(days=1))
    async with SessionLocal() as session:
        with pytest.raises(DashboardServiceUnavailableError) as caught:
            await ReportsRepository(session).list_api_key_options(NOW - timedelta(days=7), NOW)
        assert caught.value.code == "report_history_building"
        # A refusal neither mutates coverage nor poisons the session.
        assert (await session.execute(select(RequestLog.id))).scalars().all()


@pytest.mark.parametrize("recent", [False, True])
async def test_excluded_history_does_not_wait_for_a_watermark_that_cannot_advance(async_client, db_setup, recent):
    async with SessionLocal() as session:
        session.add(_make_account("coverage-account", "coverage@example.invalid"))
        await session.flush()
        for kind, account_id in (
            ("warmup", "coverage-account"),
            ("limit_warmup", "coverage-account"),
            ("normal", None),
        ):
            session.add(
                RequestLog(
                    account_id=account_id,
                    request_id=kind,
                    requested_at=NOW - timedelta(days=30),
                    request_kind=kind,
                    model="coverage-model",
                    status="success",
                )
            )
        if recent:
            session.add(
                RequestLog(
                    account_id="coverage-account",
                    request_id="recent",
                    requested_at=NOW - timedelta(minutes=30),
                    model="coverage-model",
                    status="success",
                )
            )
        await session.commit()
    assert await run_fold_pass(now=NOW) == 0
    response = await async_client.get("/api/accounts")
    assert response.status_code == 200
    usage = response.json()["accounts"][0]["requestUsage"]
    if recent:
        assert usage["requestCount"] == 1
    else:
        assert usage is None


@pytest.mark.parametrize("ids", [None, ["coverage-account"]])
async def test_account_read_skips_large_excluded_prefix_with_countable_index(db_setup, ids):
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite VM work bound")
    await _seed(NOW - timedelta(days=30), count=19998)
    async with SessionLocal() as session:
        from sqlalchemy import update

        await session.execute(update(RequestLog).values(request_kind="warmup"))
        session.add(_make_account("other-account", "other@example.invalid"))
        await session.flush()
        session.add(
            RequestLog(
                account_id="other-account",
                request_id="other-live",
                requested_at=NOW,
                model="coverage-model",
                status="success",
            )
        )
        session.add(
            RequestLog(
                account_id="coverage-account",
                request_id="live",
                requested_at=NOW,
                request_kind="future-kind",
                model="coverage-model",
                status="success",
            )
        )
        await session.commit()
        connection = await session.connection()
        driver = cast(AsyncSQLiteConnection, (await connection.get_raw_connection()).driver_connection)
        operations = 0

        def progress():
            nonlocal operations
            operations += 100
            return int(operations > 5000)

        await driver.set_progress_handler(progress, 100)
        try:
            totals = await AccountsRepository(session).list_request_usage_summary_by_account(ids)
            assert totals["coverage-account"].request_count == 1
            assert set(totals) == ({"coverage-account"} if ids else {"coverage-account", "other-account"})
        finally:
            await driver.set_progress_handler(lambda: 0, 0)
        assert operations <= 5000
        # Unknown non-warmup kinds remain eligible; do not enumerate only the
        # currently known producers and silently omit future traffic kinds.
        await session.execute(
            update(RequestLog).where(RequestLog.request_id == "live").values(requested_at=NOW - timedelta(days=1))
        )
        with pytest.raises(DashboardServiceUnavailableError) as caught:
            await require_bounded_account_history(session, None, NOW - timedelta(hours=4))
        assert caught.value.code == "usage_history_building"


@pytest.mark.parametrize("fold_history", [False, True])
async def test_accounts_route_large_history_uses_bounded_tail(async_client, db_setup, monkeypatch, fold_history):
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite VM work bound")
    await _seed(NOW - timedelta(days=30), count=19997)
    async with SessionLocal() as session:
        from sqlalchemy import update

        if not fold_history:
            await session.execute(update(RequestLog).values(request_kind="warmup"))
        session.add(_make_account("other-account", "other@example.invalid"))
        await session.flush()
        session.add_all(
            [
                RequestLog(
                    account_id="other-account",
                    request_id="other-folded",
                    requested_at=NOW - timedelta(days=30),
                    request_kind="normal" if fold_history else "warmup",
                    model="coverage-model",
                    status="success",
                ),
                RequestLog(
                    account_id="other-account",
                    request_id="other-live",
                    requested_at=NOW,
                    model="coverage-model",
                    status="success",
                ),
            ]
        )
        session.add(
            RequestLog(
                account_id="coverage-account",
                request_id="live",
                requested_at=NOW,
                request_kind="future-kind",
                model="coverage-model",
                status="success",
            )
        )
        await session.commit()
    if fold_history:
        assert await run_fold_pass(now=NOW) > 0
    operations = 0
    original = dashboard_reads._ReadBudget.progress

    def progress(budget):
        nonlocal operations
        operations += 1
        return int(operations > 25000 or original(budget))

    monkeypatch.setattr(dashboard_reads, "_PROGRESS_STEPS", 1)
    monkeypatch.setattr(dashboard_reads._ReadBudget, "progress", progress)
    response = await async_client.get("/api/accounts")
    assert response.status_code == 200, response.json()
    counts = {row["accountId"]: row["requestUsage"]["requestCount"] for row in response.json()["accounts"]}
    assert counts == {"coverage-account": 19998 if fold_history else 1, "other-account": 2 if fold_history else 1}
    assert 0 < operations <= 25000
    async with SessionLocal() as session:
        scoped = await AccountsRepository(session).list_request_usage_summary_by_account(["coverage-account"])
        assert set(scoped) == {"coverage-account"}
        assert scoped["coverage-account"].request_count == counts["coverage-account"]
