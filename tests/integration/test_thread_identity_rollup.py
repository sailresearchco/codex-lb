from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from time import perf_counter
from typing import cast

import pytest
from aiosqlite import Connection as AsyncSQLiteConnection
from sqlalchemy import delete, func, insert, select, update

from app.core.exceptions import DashboardServiceUnavailableError
from app.db.models import RequestLog, ThreadIdentityDirtyHour, ThreadIdentityHour
from app.db.session import SessionLocal
from app.modules.accounts.repository import AccountsRepository
from app.modules.accounts.usage_rollup import lock_fold_state
from app.modules.accounts.usage_time_rollup import merge_time_rollups_into
from app.modules.reports.cache import ReportsCaches
from app.modules.reports.service import _thread_identity_facet
from app.modules.reports.thread_identity import aggregate_thread_identity
from app.modules.reports.thread_rollup import evict_pruned_thread_hours, fold_next_thread_slice, mark_thread_hours_dirty
from app.modules.reports.thread_rollup_read import aggregate_cached_thread_identity
from app.modules.request_logs.repository import RequestLogsRepository
from tests.integration.test_reports_api import _make_account

pytestmark = pytest.mark.integration
START = datetime(2026, 6, 1)
END = START + timedelta(days=1)


async def _seed() -> None:
    async with SessionLocal() as session:
        session.add_all([_make_account(account, f"{account}@example.com") for account in ("a", "b", "c")])
        await session.flush()
        for i, (seconds, account, conv, sid, key, tokens, status) in enumerate(
            (
                (3599, "a", "thread", None, "key", 10000, "success"),
                (3600, "b", None, "thread", "other-key", None, "error"),
                (3601, "c", "thread", None, "key", 5000, "success"),
                (3601, "a", "thread", None, "key", 4000, "success"),
                (100, "a", " ", "\t", "approx", 6000, "success"),
                (110, "b", None, None, "approx", 5000, "success"),
                (120, "a", None, None, "approx", 7000, "success"),
                (130, None, None, None, "approx", 8000, "success"),
                (140, "c", None, None, "approx", 9000, "success"),
                (150, "a", None, None, None, 10000, "success"),
                (151, "b", None, None, None, 11000, "success"),
                (152, "c", None, None, None, 12000, "success"),
                (4000, None, "detached", None, "key", 12000, "success"),
                (4001, "a", "detached", None, "key", 13000, "success"),
                (4002, "a", "detached", None, "key", 14000, "success"),
            )
        ):
            session.add(
                RequestLog(
                    request_id=f"thread-{i}",
                    requested_at=START + timedelta(seconds=seconds),
                    account_id=account,
                    conversation_id=conv,
                    session_id=sid,
                    api_key_id=key,
                    input_tokens=tokens,
                    cached_input_tokens=3000 if i % 2 else None,
                    status=status,
                    model="m",
                    deleted_at=END if account is None else None,
                )
            )
        session.add(
            RequestLog(
                request_id="warm",
                requested_at=START + timedelta(seconds=3600),
                account_id="a",
                conversation_id="thread",
                source="limit_warmup",
                model="m",
                status="success",
            )
        )
        await session.commit()


async def _fold(target: datetime = END) -> None:
    async with SessionLocal() as session:
        for _ in range(32):
            if not await fold_next_thread_slice(session, target):
                return
        pytest.fail("small fixture did not finish its bounded fold")


async def _parity(start=START, end=END):
    async with SessionLocal() as session:
        expected = await aggregate_thread_identity(session, start, end)
        actual = await aggregate_cached_thread_identity(session, start, end)
    assert {key: asdict(value) for key, value in actual.items()} == {
        key: asdict(value) for key, value in expected.items()
    }
    return actual


async def test_route_matches_raw_identity_across_hour_boundaries(async_client, app_instance):
    await _seed()
    await _fold()
    facets = await _parity()
    app_instance.state.reports_caches = ReportsCaches()
    response = await async_client.get(
        "/api/reports/thread-identity", params={"start_date": "2026-06-01", "end_date": "2026-06-01", "timezone": "UTC"}
    )
    assert response.status_code == 200
    total = sum(row.requests for row in facets.values())
    for keyed, name in ((True, "keyed"), (False, "unkeyed")):
        assert response.json()[name] == _thread_identity_facet(facets[keyed], total, approximate=not keyed).model_dump(
            by_alias=True
        )


@pytest.mark.parametrize(
    "start,end",
    [
        (START + timedelta(minutes=30), START + timedelta(hours=2)),
        (START, START + timedelta(hours=1, seconds=1)),
        (START + timedelta(seconds=3599, microseconds=1), START + timedelta(hours=2)),
    ],
)
async def test_partial_hours_and_microsecond_boundaries(db_setup, start, end):
    await _seed()
    await _fold()
    await _parity(start, end)


@pytest.mark.parametrize("delete_history", [False, True])
async def test_account_deletion_rebuilds_new_adjacency(async_client, app_instance, delete_history):
    await _seed()
    await _fold()
    params = {"start_date": "2026-06-01", "end_date": "2026-06-01", "timezone": "UTC"}
    assert (await async_client.get("/api/reports/thread-identity", params=params)).status_code == 200
    async with SessionLocal() as session:
        assert await AccountsRepository(session).delete("b", delete_history=delete_history)
    response = await async_client.get(
        "/api/reports/thread-identity", params={"start_date": "2026-06-01", "end_date": "2026-06-01", "timezone": "UTC"}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "thread_identity_repairing"
    await _fold()
    facets = await _parity()
    repaired = await async_client.get("/api/reports/thread-identity", params=params)
    assert repaired.status_code == 200
    assert repaired.json()["keyed"]["turns"] == facets[True].turns
    # Hard deletion links a->c across the removed b, while soft detach leaves
    # an unattributed request in the sequence and must not create that link.
    assert facets[True].account_switches == (2 if delete_history else 1)


async def test_account_merge_rekeys_pairs_without_losing_turns(db_setup):
    await _seed()
    await _fold()
    async with SessionLocal() as session:
        await lock_fold_state(session)
        await session.execute(update(RequestLog).where(RequestLog.account_id == "b").values(account_id="a"))
        await merge_time_rollups_into(session, "a", ["b"])
        await session.commit()
    await _parity()


async def test_late_append_marks_folded_hour_then_repairs(async_client):
    await _seed()
    await _fold()
    params = {"start_date": "2026-06-01", "end_date": "2026-06-01", "timezone": "UTC"}
    initial = await async_client.get("/api/reports/thread-identity", params=params)
    assert initial.status_code == 200
    async with SessionLocal() as session:
        await RequestLogsRepository(session).add_log(
            account_id="b",
            api_key_id="key",
            request_id="late",
            model="m",
            status="success",
            requested_at=START + timedelta(seconds=3600, microseconds=1),
            conversation_id="thread",
            input_tokens=15000,
            output_tokens=None,
            latency_ms=None,
            error_code=None,
        )
    async with SessionLocal() as session:
        with pytest.raises(DashboardServiceUnavailableError) as error:
            await aggregate_cached_thread_identity(session, START, END)
        assert error.value.code == "thread_identity_repairing"
    waiting = await async_client.get("/api/reports/thread-identity", params=params)
    assert waiting.status_code == 503
    assert waiting.json()["error"]["code"] == "thread_identity_repairing"
    await _fold()
    await _parity()
    repaired = await async_client.get("/api/reports/thread-identity", params=params)
    assert repaired.status_code == 200
    assert repaired.json()["totalRequests"] == initial.json()["totalRequests"] + 1


async def test_partial_retention_and_failed_repair_never_publish_stale_counts(db_setup, monkeypatch):
    await _seed()
    await _fold()
    async with SessionLocal() as session:
        await lock_fold_state(session)
        ids = list(
            (
                await session.scalars(
                    select(RequestLog.id).where(RequestLog.requested_at < START + timedelta(seconds=3601))
                )
            ).all()
        )
        await mark_thread_hours_dirty(session, ids)
        await session.execute(delete(RequestLog).where(RequestLog.id.in_(ids)))
        await evict_pruned_thread_hours(session)
        await session.commit()
    async with SessionLocal() as session:
        with pytest.raises(DashboardServiceUnavailableError):
            await aggregate_cached_thread_identity(session, START, END)
        before = await session.scalar(select(func.count()).select_from(ThreadIdentityHour))
    import app.modules.reports.thread_rollup as rollup

    original = rollup.hour_sources
    monkeypatch.setattr(rollup, "hour_sources", lambda *_: (_ for _ in ()).throw(RuntimeError("interrupted")))
    async with SessionLocal() as session:
        with pytest.raises(RuntimeError, match="interrupted"):
            await fold_next_thread_slice(session, END)
        await session.rollback()
        assert await session.scalar(select(func.count()).select_from(ThreadIdentityHour)) == before
        assert await session.scalar(select(ThreadIdentityDirtyHour.bucket_epoch).limit(1)) is not None
    monkeypatch.setattr(rollup, "hour_sources", original)
    await _fold()
    await _parity()


@pytest.mark.parametrize("folded", [False, True])
async def test_cold_and_folded_reads_have_bounded_sqlite_work(db_setup, folded, record_property):
    async with SessionLocal() as session:
        if session.get_bind().dialect.name != "sqlite":
            pytest.skip("SQLite VM budget regression")
        await session.execute(
            insert(RequestLog),
            [
                {
                    "request_id": f"dense-{i}",
                    "requested_at": START + timedelta(seconds=i % 3000),
                    "conversation_id": "dense",
                    "model": "m",
                    "status": "success",
                    "input_tokens": 6000,
                }
                for i in range(10000)
            ],
        )
        await session.commit()
    async with SessionLocal() as session:
        session.add(
            RequestLog(
                request_id="live-tail",
                requested_at=END - timedelta(minutes=90),
                conversation_id="dense",
                model="m",
                status="success",
                input_tokens=6000,
            )
        )
        await session.commit()
    if folded:
        await _fold(END - timedelta(hours=2))
    async with SessionLocal() as session:
        connection = await session.connection()
        driver = cast(AsyncSQLiteConnection, (await connection.get_raw_connection()).driver_connection)
        operations = 0

        def progress():
            nonlocal operations
            operations += 100
            return int(operations > 50000)

        await driver.set_progress_handler(progress, 100)
        started = perf_counter()
        try:
            if folded:
                assert (await aggregate_cached_thread_identity(session, START, END))[True].requests == 10001
            else:
                with pytest.raises(DashboardServiceUnavailableError) as error:
                    await aggregate_cached_thread_identity(session, START, END)
                assert error.value.code == "thread_identity_building"
        finally:
            await driver.set_progress_handler(lambda: 0, 0)
            record_property("read_vm_operations", operations)
            record_property("read_seconds", perf_counter() - started)
