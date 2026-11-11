from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import TypedDict, cast

import pytest
from aiosqlite import Connection as AsyncSQLiteConnection
from sqlalchemy import delete, func, insert, select, update

from app.db.models import AccountUsageRollupState, ApiKey, RequestLog, RequestReportHourlyRollup
from app.db.session import SessionLocal
from app.modules.accounts.repository import AccountsRepository
from app.modules.accounts.usage_rollup import lock_fold_state
from app.modules.accounts.usage_time_rollup import merge_time_rollups_into
from app.modules.reports.repository import ReportsRepository
from app.modules.reports.rollup import fold_next_report_slice, run_report_fold_pass
from app.modules.reports.service import ReportsService
from tests.integration.test_reports_api import _make_account

pytestmark = pytest.mark.integration
BASE = datetime(2026, 3, 1)
END = datetime(2026, 3, 22)


async def _seed() -> None:
    async with SessionLocal() as session:
        session.add_all([_make_account("report-a", "a@example.com"), _make_account("report-b", "b@example.com")])
        await session.flush()
        for i in range(160):
            session.add(
                RequestLog(
                    requested_at=BASE + timedelta(hours=i * 3, minutes=(i % 4) * 15),
                    request_id=f"report-fold-{i}",
                    account_id=("report-a", "report-b", None)[i % 3],
                    api_key_id=(None, "key", "")[i % 3],
                    model=("m1", "m2")[i % 2],
                    useragent_group=(None, "CLI", "", " ", "\x1fclient")[i % 5],
                    conversation_id=(None, " shared ", "shared", "\t\n", "other")[i % 5],
                    source="limit_warmup" if i % 13 == 0 else None,
                    request_kind="warmup" if i % 17 == 0 else "normal",
                    status=("success", "error", "cancelled")[i % 3],
                    input_tokens=i,
                    output_tokens=None if i % 4 == 0 else i * 2,
                    reasoning_tokens=3 if i % 4 == 0 else None,
                    cached_input_tokens=i % 10,
                    cost_usd=None if i % 7 == 0 else (i % 9) * 0.01,
                )
            )
        await session.commit()


async def _fold(target: datetime) -> None:
    async with SessionLocal() as session:
        while await fold_next_report_slice(session, target):
            pass


async def _snapshot(tz: str = "UTC", **filters):
    async with SessionLocal() as session:
        service = ReportsService(ReportsRepository(session))
        report = await service.get_reports(date(2026, 3, 1), date(2026, 3, 21), tz, **filters)
        options = await service.get_options(
            date(2026, 3, 1), date(2026, 3, 21), tz, filters.get("account_ids"), filters.get("api_key_ids")
        )
        return report.model_dump(exclude={"generated_at"}), options.model_dump()


@pytest.mark.parametrize("tz", ["UTC", "Asia/Kolkata", "America/New_York"])
async def test_report_fold_preserves_filters_timezone_and_live_tail(db_setup, tz):
    await _seed()
    scopes = [
        {},
        {"account_ids": ["report-a"]},
        {"api_key_ids": ["key"], "model": "m1"},
        {"useragent_group": "Missing User-Agent"},
        {"useragent_group": "\x1fclient"},
    ]
    expected = [await _snapshot(tz, **scope) for scope in scopes]
    await _fold(BASE + timedelta(days=8))
    assert [await _snapshot(tz, **scope) for scope in scopes] == expected
    await _fold(END)
    assert [await _snapshot(tz, **scope) for scope in scopes] == expected
    # A completed fold is idempotent, including the exact first activity value.
    await _fold(END)
    assert [await _snapshot(tz, **scope) for scope in scopes] == expected


async def test_report_statistics_survive_raw_pruning(db_setup):
    await _seed()
    await _fold(END)
    expected = await _snapshot()
    async with SessionLocal() as session:
        await session.execute(delete(RequestLog).where(RequestLog.requested_at < END))
        await session.commit()
    assert await _snapshot() == expected


async def test_earliest_report_activity_preserves_filtered_minima_and_raw_boundary(db_setup):
    async with SessionLocal() as session:
        session.add_all([_make_account("report-a", "a@example.com"), _make_account("report-b", "b@example.com")])
        await session.flush()
        for minutes, account, key, model, agent, deleted, kind, source in (
            (40, "report-a", "removed", "m2", "CLI", False, "normal", None),
            (5, "report-b", "\x1fkey", "m1", None, True, "normal", None),
            (20, "report-a", "", "m2", "", False, "normal", None),
            (60, "report-a", "removed", "m1", "\x1fclient", False, "normal", None),
            (130, "report-b", None, "m3", "CLI", False, "normal", None),
            (1, "report-a", None, "m1", "CLI", False, "normal", "limit_warmup"),
            (2, "report-a", None, "m1", "CLI", False, "warmup", None),
        ):
            session.add(
                RequestLog(
                    request_id=f"earliest-{minutes}",
                    requested_at=BASE + timedelta(minutes=minutes),
                    account_id=account,
                    api_key_id=key,
                    model=model,
                    useragent_group=agent,
                    deleted_at=BASE if deleted else None,
                    request_kind=kind,
                    source=source,
                    status="success",
                )
            )
        await session.commit()

    class EarliestFilters(TypedDict, total=False):
        account_ids: list[str]
        api_key_ids: list[str]
        model: str
        useragent_group: str

    scopes: list[tuple[EarliestFilters, int | None]] = [
        ({}, 5),
        ({"account_ids": ["report-a"]}, 20),
        ({"api_key_ids": ["\x1fkey"]}, 5),
        ({"api_key_ids": [""]}, 20),
        ({"api_key_ids": ["removed"], "model": "m1"}, 60),
        ({"useragent_group": "Missing User-Agent"}, 5),
        ({"useragent_group": "\x1fclient"}, 60),
        ({"model": "m3"}, 130),
        ({"account_ids": ["missing"]}, None),
    ]

    async def check_minima():
        async with SessionLocal() as session:
            repo = ReportsRepository(session)
            for filters, minutes in scopes:
                expected = None if minutes is None else BASE + timedelta(minutes=minutes)
                assert await repo.earliest_report_activity_at(**filters) == expected

    await check_minima()
    await _fold(BASE + timedelta(hours=1))
    await check_minima()
    async with SessionLocal() as session:
        await session.execute(delete(RequestLog).where(RequestLog.requested_at < BASE + timedelta(hours=1)))
        await session.commit()
    await check_minima()
    await _fold(BASE + timedelta(hours=3))
    await check_minima()


async def test_earliest_report_activity_has_bounded_sqlite_work(db_setup):
    row_count = 10_000
    async with SessionLocal() as session:
        if session.get_bind().dialect.name != "sqlite":
            pytest.skip("SQLite virtual-machine work regression")
        await session.execute(
            insert(RequestLog),
            [
                {
                    "request_id": f"work-{index}",
                    "requested_at": BASE + timedelta(minutes=index),
                    "conversation_id": f"thread-{index}",
                    "api_key_id": f"key-{index % 64}",
                    "model": f"model-{index % 4}",
                    "useragent_group": f"client-{index % 8}",
                    "input_tokens": index,
                    "status": "success",
                }
                for index in range(row_count)
            ],
        )
        await session.commit()

    async def bounded_lookup():
        async with SessionLocal() as session:
            connection = await session.connection()
            driver = cast(AsyncSQLiteConnection, (await connection.get_raw_connection()).driver_connection)
            operations = 0

            def progress() -> int:
                nonlocal operations
                operations += 100
                # A generous indexed-lookup budget. Grouping these 10,000
                # historical threads for one MIN exceeds it by orders of magnitude.
                return int(operations > 50_000)

            await driver.set_progress_handler(progress, 100)
            try:
                assert await ReportsRepository(session).earliest_report_activity_at() == BASE
            finally:
                await driver.set_progress_handler(lambda: 0, 0)
            completed_operations = operations
            # Reuse the same connection for a full scan and verify the
            # private measurement callback cannot leak to later work.
            assert await session.scalar(select(func.sum(RequestLog.input_tokens))) == row_count * (row_count - 1) // 2
            assert operations == completed_operations

    await bounded_lookup()  # No state row: cold raw fallback.
    await _fold(BASE + timedelta(days=7))
    await bounded_lookup()  # Permanent history with high thread cardinality.


async def test_report_key_catalog_retains_historical_keys_in_selected_period(db_setup):
    async with SessionLocal() as session:
        session.add(ApiKey(id="unused-current", name="Current", key_hash="current-hash", key_prefix="cur"))
        for key, at in (
            ("deleted-folded", BASE + timedelta(minutes=15)),
            ("\x1fdeleted-tail", BASE + timedelta(hours=1)),
            (None, BASE + timedelta(minutes=20)),
            ("", BASE + timedelta(minutes=25)),
            ("outside-before", BASE - timedelta(microseconds=1)),
            ("outside-after", BASE + timedelta(hours=2)),
        ):
            session.add(
                RequestLog(api_key_id=key, request_id=f"catalog-{key}", requested_at=at, model="m", status="success")
            )
        await session.commit()

    async def catalog():
        async with SessionLocal() as session:
            rows = await ReportsRepository(session).list_api_key_options(BASE, BASE + timedelta(hours=2))
            return {key: prefix for key, _name, prefix in rows}

    expected = {"unused-current": "cur", "deleted-folded": None, "\x1fdeleted-tail": None}
    assert await catalog() == expected
    await _fold(BASE + timedelta(hours=1))
    assert await catalog() == expected
    async with SessionLocal() as session:
        await session.execute(delete(RequestLog).where(RequestLog.requested_at < BASE + timedelta(hours=1)))
        await session.commit()
    assert await catalog() == expected


@pytest.mark.parametrize("action", ["soft", "hard", "merge"])
async def test_report_account_lifecycle_matches_raw_history(db_setup, action):
    await _seed()
    await _fold(END)
    async with SessionLocal() as session:
        if action == "merge":
            await lock_fold_state(session)
            await merge_time_rollups_into(session, "report-b", ["report-a"])
            await session.execute(
                update(RequestLog).where(RequestLog.account_id == "report-a").values(account_id="report-b")
            )
            await session.commit()
        else:
            await AccountsRepository(session).delete("report-a", delete_history=action == "hard")
    folded = await _snapshot()
    # Reset only the report aggregate while raw history is still present.
    async with SessionLocal() as session:
        await session.execute(delete(RequestReportHourlyRollup))
        await session.execute(update(AccountUsageRollupState).values(reports_folded_through=datetime(1970, 1, 1)))
        await session.commit()
    assert await _snapshot() == folded


async def test_report_fold_failure_rolls_back_and_restarts(db_setup, monkeypatch):
    await _seed()
    import app.modules.reports.rollup as module

    original = module.report_fold_insert
    monkeypatch.setattr(module, "report_fold_insert", lambda *_: (_ for _ in ()).throw(RuntimeError("fold failed")))
    with pytest.raises(RuntimeError, match="fold failed"):
        await _fold(END)
    async with SessionLocal() as session:
        assert (await session.execute(select(func.count()).select_from(RequestReportHourlyRollup))).scalar_one() == 0
        state = await session.get(AccountUsageRollupState, 1)
        assert state is not None
        assert state.reports_folded_through == datetime(1970, 1, 1)
    monkeypatch.setattr(module, "report_fold_insert", original)
    expected = await _snapshot()
    await _fold(END)
    assert await _snapshot() == expected


async def test_concurrent_report_folds_do_not_double_count(db_setup):
    await _seed()
    expected = await _snapshot()
    await asyncio.gather(_fold(END), _fold(END))
    assert await _snapshot() == expected


async def test_report_fold_pass_is_bounded_and_respects_lag(db_setup):
    await _seed()
    import app.modules.reports.rollup as module

    committed = await run_report_fold_pass(now=END + module.FOLD_LAG)
    assert committed == module.REPORT_MAX_SLICES
    async with SessionLocal() as session:
        state = await session.get(AccountUsageRollupState, 1)
        assert state is not None
        assert state.reports_folded_through <= BASE + module.REPORT_FOLD_SLICE * module.REPORT_MAX_SLICES
    await _fold(END)
    assert await run_report_fold_pass(now=END + module.FOLD_LAG) == 0


async def test_retention_waits_for_report_watermark(db_setup):
    from app.core.retention.job import _prune_request_logs
    from app.modules.accounts.usage_rollup import FOLD_LAG

    await _seed()
    now = END + FOLD_LAG
    async with SessionLocal() as session:
        session.add(
            AccountUsageRollupState(
                id=1,
                folded_through=END,
                hourly_folded_through=END,
                conversation_folded_through=END,
                speed_folded_through=END,
            )
        )
        await session.commit()
    assert await _prune_request_logs(END - FOLD_LAG, now=now) == 0
    await _fold(END)
    expected = await _snapshot()
    assert await _prune_request_logs(END - FOLD_LAG, now=now) > 0
    assert await _snapshot() == expected


async def test_report_rollup_migration_roundtrip(tmp_path):
    from alembic import command
    from anyio import to_thread
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.migrate import _build_alembic_config, run_upgrade

    url = f"sqlite+aiosqlite:///{tmp_path / 'reports.sqlite'}"
    parent = "20260909_050000_dashboard_routing_overload_settings"
    await to_thread.run_sync(lambda: run_upgrade(url, parent, bootstrap_legacy=False))
    await to_thread.run_sync(lambda: run_upgrade(url, "head", bootstrap_legacy=False))
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            assert await conn.run_sync(lambda c: inspect(c).has_table("request_report_hourly_rollups"))
        await to_thread.run_sync(lambda: command.downgrade(_build_alembic_config(url), parent))
        async with engine.connect() as conn:
            assert not await conn.run_sync(lambda c: inspect(c).has_table("request_report_hourly_rollups"))
        await to_thread.run_sync(lambda: run_upgrade(url, "head", bootstrap_legacy=False))
        # Guarded upgrade tolerates the already-created table and column.
        from importlib import import_module

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        revision = import_module("app.db.alembic.versions.20260909_060000_add_report_rollup")
        async with engine.begin() as conn:

            def upgrade_again(c):
                with Operations.context(MigrationContext.configure(c)):
                    revision.upgrade()

            await conn.run_sync(upgrade_again)
    finally:
        await engine.dispose()


async def test_model_rewrite_skips_report_history_but_updates_exact_watermark_tail(db_setup):
    from app.modules.request_logs.repository import RequestLogsRepository

    await _seed()
    await _fold(END)
    async with SessionLocal() as session:
        session.add(RequestLog(request_id="report-fold-1", requested_at=END, model="m-live", status="success"))
        await session.commit()
        assert await RequestLogsRepository(session).update_model_for_request("report-fold-1", "gpt-5.1") == 1
        rows = (
            (
                await session.execute(
                    select(RequestLog.model)
                    .where(RequestLog.request_id == "report-fold-1")
                    .order_by(RequestLog.requested_at)
                )
            )
            .scalars()
            .all()
        )
        assert rows == ["m2", "gpt-5.1"]
