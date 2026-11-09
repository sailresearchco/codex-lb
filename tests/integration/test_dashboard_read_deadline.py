from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event, RLock
from typing import cast

import pytest
import pytest_asyncio
from aiosqlite import Connection as AsyncSQLiteConnection
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.exceptions import DashboardServiceUnavailableError
from app.core.utils.time import utcnow
from app.db import dashboard_reads
from app.db.dashboard_reads import (
    dashboard_read_budget,
    run_sqlite_usage_read,
    sqlite_usage_read_connection,
    sqlite_usage_read_lock,
)
from app.db.models import Account, AccountStatus, ApiKey
from app.db.session import SessionLocal
from app.modules.accounts.repository import AccountsRepository
from app.modules.api_keys.repository import ApiKeysRepository
from app.modules.dashboard.repository import DashboardRepository
from app.modules.reports.repository import ReportsRepository
from app.modules.request_logs.repository import RequestLogsRepository

pytestmark = pytest.mark.integration
_ROWS = "WITH RECURSIVE rows(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM rows WHERE n < 100000000)"
_EXPENSIVE_QUERY = f"{_ROWS} SELECT sum(n) FROM rows"


@pytest_asyncio.fixture
async def read_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'read-budget.sqlite'}", pool_size=1, max_overflow=1)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE writes (value INTEGER NOT NULL)"))
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_read_deadline_interrupts_sqlite_and_restores_connection(read_engine, monkeypatch):
    monkeypatch.setattr(dashboard_reads, "_READ_BUDGET_SECONDS", 0.02)
    async with AsyncSession(read_engine) as session:
        connection = await session.connection()
        raw = await connection.get_raw_connection()
        driver = cast(AsyncSQLiteConnection, raw.driver_connection)
        await connection.execute(text("PRAGMA busy_timeout=1234"))
        with pytest.raises(DashboardServiceUnavailableError) as caught:
            async with dashboard_read_budget(session):
                await session.execute(text(_EXPENSIVE_QUERY))
        assert caught.value.code == "dashboard_query_timeout"
        assert isinstance(caught.value.__cause__, OperationalError)
        assert isinstance(caught.value.__cause__.orig, sqlite3.OperationalError)
        assert caught.value.__cause__.orig.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
        # Reuse the exact connection after interruption; the expired handler
        # must not poison later reads, writes, or the original lock-wait setting.
        assert (await session.connection()) is connection
        assert (await connection.get_raw_connection()).driver_connection is driver
        assert await session.scalar(text("PRAGMA busy_timeout")) == 1234
        await session.execute(text("INSERT INTO writes VALUES (7)"))
        await session.commit()
        assert await session.scalar(text("SELECT value FROM writes")) == 7


@pytest.mark.asyncio
async def test_read_deadline_covers_rows_fetched_after_execute(read_engine, monkeypatch):
    monkeypatch.setattr(dashboard_reads, "_READ_BUDGET_SECONDS", 0.03)
    async with AsyncSession(read_engine) as session:
        with pytest.raises(DashboardServiceUnavailableError) as caught:
            async with dashboard_read_budget(session):
                connection = await session.connection()
                driver = cast(AsyncSQLiteConnection, (await connection.get_raw_connection()).driver_connection)
                async with driver.execute(f"{_ROWS} SELECT n FROM rows") as cursor:
                    assert await cursor.fetchone() == (1,)
                    await cursor.fetchall()
        assert caught.value.code == "dashboard_query_timeout"
        assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
        assert caught.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
        assert await session.scalar(text("SELECT 1")) == 1


@pytest.mark.asyncio
async def test_read_budget_is_cumulative_and_does_not_limit_another_writer(read_engine, monkeypatch):
    monkeypatch.setattr(dashboard_reads, "_READ_BUDGET_SECONDS", 0.02)
    async with AsyncSession(read_engine) as session:
        with pytest.raises(DashboardServiceUnavailableError) as caught:
            async with dashboard_read_budget(session):
                assert await session.scalar(text("SELECT 1")) == 1
                await asyncio.sleep(0.03)
                async with AsyncSession(read_engine) as writer:
                    await writer.execute(text("INSERT INTO writes VALUES (9)"))
                    await writer.commit()
                await session.execute(text("SELECT 2"))
        assert caught.value.code == "dashboard_query_timeout"
        assert await session.scalar(text("SELECT value FROM writes")) == 9


@pytest.mark.asyncio
async def test_later_statement_lock_wait_uses_remaining_budget(read_engine, monkeypatch):
    monkeypatch.setattr(dashboard_reads, "_READ_BUDGET_SECONDS", 0.04)
    blocker = sqlite3.connect(str(read_engine.url.database))
    try:
        async with AsyncSession(read_engine) as session:
            with pytest.raises(DashboardServiceUnavailableError) as caught:
                async with dashboard_read_budget(session):
                    await session.execute(text("SELECT 1"))
                    await asyncio.sleep(0.02)
                    blocker.execute("BEGIN EXCLUSIVE")
                    await session.execute(text("SELECT value FROM writes"))
            assert caught.value.code == "dashboard_query_timeout"
            assert isinstance(caught.value.__cause__, OperationalError)
            assert isinstance(caught.value.__cause__.orig, sqlite3.OperationalError)
            assert caught.value.__cause__.orig.sqlite_errorcode == sqlite3.SQLITE_BUSY
            blocker.rollback()
            assert await session.scalar(text("SELECT count(*) FROM writes")) == 0
    finally:
        blocker.close()


@pytest.mark.asyncio
async def test_usage_worker_shares_budget_and_finishes_before_error_returns(read_engine, monkeypatch):
    monkeypatch.setattr(dashboard_reads, "_READ_BUDGET_SECONDS", 0.03)
    finished = Event()

    def usage_read() -> None:
        try:
            with sqlite_usage_read_connection(str(read_engine.url.database)) as connection:
                connection.execute(_EXPENSIVE_QUERY).fetchall()
        finally:
            finished.set()

    async with AsyncSession(read_engine) as session:
        with pytest.raises(DashboardServiceUnavailableError) as caught:
            async with dashboard_read_budget(session):
                await session.execute(text("SELECT 1"))
                await run_sqlite_usage_read(usage_read)
        assert caught.value.code == "dashboard_query_timeout"
        assert finished.is_set()
        # Outside the read scope, the existing synchronous reader is unaffected.
        with sqlite_usage_read_connection(str(read_engine.url.database)) as connection:
            assert connection.execute("SELECT 1").fetchone() == (1,)


@pytest.mark.asyncio
async def test_cancelled_read_finishes_owned_sqlite_work(read_engine, monkeypatch):
    monkeypatch.setattr(dashboard_reads, "_READ_BUDGET_SECONDS", 0.05)
    started = asyncio.Event()

    async def read() -> None:
        async with AsyncSession(read_engine) as session:
            async with dashboard_read_budget(session):
                started.set()
                await session.execute(text(_EXPENSIVE_QUERY))

    task = asyncio.create_task(read())
    try:
        await started.wait()
        await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        async with AsyncSession(read_engine) as session:
            await session.execute(text("INSERT INTO writes VALUES (11)"))
            await session.commit()
            assert await session.scalar(text("SELECT value FROM writes")) == 11
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_read_session_keeps_deadline_after_repository_commit(read_engine, monkeypatch):
    monkeypatch.setattr(dashboard_reads, "engine", read_engine)
    monkeypatch.setattr(dashboard_reads, "SessionLocal", async_sessionmaker(read_engine, expire_on_commit=False))
    monkeypatch.setattr(dashboard_reads, "_READ_BUDGET_SECONDS", 0.03)
    with pytest.raises(DashboardServiceUnavailableError) as caught:
        async with asynccontextmanager(dashboard_reads.get_dashboard_read_session)() as session:
            connection = await session.connection()
            driver = (await connection.get_raw_connection()).driver_connection
            await session.execute(text("INSERT INTO writes VALUES (13)"))
            await session.commit()
            current_connection = await session.connection()
            assert (await current_connection.get_raw_connection()).driver_connection is driver
            await session.execute(text(_EXPENSIVE_QUERY))
    assert caught.value.code == "dashboard_query_timeout"
    async with AsyncSession(read_engine) as session:
        assert await session.scalar(text("SELECT value FROM writes")) == 13


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_on", ["sqlite", "cache"])
async def test_cancelled_usage_read_drains_worker_blocked_on_lock(read_engine, monkeypatch, blocked_on):
    monkeypatch.setattr(dashboard_reads, "_READ_BUDGET_SECONDS", 0.05)
    started = asyncio.Event()
    finished = Event()
    loop = asyncio.get_running_loop()
    cache_lock = RLock()
    blocker = sqlite3.connect(str(read_engine.url.database))
    cache_lock.acquire()

    def usage_read() -> None:
        try:
            with sqlite_usage_read_connection(str(read_engine.url.database)) as connection:
                loop.call_soon_threadsafe(started.set)
                if blocked_on == "cache":
                    with sqlite_usage_read_lock(cache_lock):
                        connection.execute("SELECT value FROM writes").fetchall()
                else:
                    connection.execute("SELECT value FROM writes").fetchall()
        finally:
            finished.set()

    async def read() -> None:
        async with AsyncSession(read_engine) as session:
            async with dashboard_read_budget(session):
                if blocked_on == "sqlite":
                    blocker.execute("BEGIN EXCLUSIVE")
                await run_sqlite_usage_read(usage_read)

    task = asyncio.create_task(read())
    try:
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert finished.is_set()
    finally:
        blocker.close()
        cache_lock.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "repository", "method"),
    [
        ("/api/dashboard/overview", DashboardRepository, "list_accounts"),
        ("/api/request-logs", RequestLogsRepository, "list_recent"),
        ("/api/reports", ReportsRepository, "aggregate_summary"),
        ("/api/accounts", AccountsRepository, "list_accounts"),
        ("/api/accounts/synthetic/trends", AccountsRepository, "get_by_id"),
        ("/api/accounts/synthetic/rate-limit-reset-credits", AccountsRepository, "get_by_id"),
    ],
)
async def test_dashboard_routes_return_structured_query_timeout(
    async_client, db_setup, monkeypatch, path, repository, method
):
    from app.db.session import engine

    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite query budget; PostgreSQL uses its existing driver deadline")
    monkeypatch.setattr(dashboard_reads, "_READ_BUDGET_SECONDS", 0.02)

    async def slow_read(self, *args, **kwargs):
        await self._session.execute(text(_EXPENSIVE_QUERY))
        raise AssertionError("Unbounded query completed")

    monkeypatch.setattr(repository, method, slow_read)
    response = await async_client.get(path)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "dashboard_query_timeout"


@pytest.mark.asyncio
async def test_account_mutation_keeps_its_existing_session_without_read_deadline(async_client, db_setup, monkeypatch):
    async with SessionLocal() as session:
        session.add(
            Account(
                id="deadline-writer",
                email="synthetic@example.invalid",
                plan_type="plus",
                access_token_encrypted=b"synthetic",
                refresh_token_encrypted=b"synthetic",
                id_token_encrypted=b"synthetic",
                last_refresh=utcnow(),
                status=AccountStatus.ACTIVE,
            )
        )
        await session.commit()
    monkeypatch.setattr(dashboard_reads, "_READ_BUDGET_SECONDS", -1)
    response = await async_client.patch("/api/accounts/deadline-writer", json={"securityWorkAuthorized": True})
    assert response.status_code == 200
    async with SessionLocal() as session:
        assert await session.scalar(select(Account.security_work_authorized).where(Account.id == "deadline-writer"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/api-keys", "list_usage_summary_by_key"),
        ("/api/api-keys/", "list_usage_summary_by_key"),
        ("/api/api-keys/{key_id}/trends", "trends_by_key"),
        ("/api/api-keys/{key_id}/usage-7d", "usage_7d"),
    ],
)
async def test_api_key_reads_interrupt_sqlite_and_allow_subsequent_reads(
    async_client, db_setup, monkeypatch, path, method
):
    from app.db.session import engine

    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite query budget; PostgreSQL uses its existing driver deadline")
    assert engine.url.database not in (None, "", ":memory:")
    created = await async_client.post("/api/api-keys", json={"name": "deadline-reader"})
    assert created.status_code == 200
    key_id = created.json()["id"]
    path = path.format(key_id=key_id)
    interrupted: list[int] = []

    async def slow_read(self, *args, **kwargs):
        try:
            await self._session.execute(text(_EXPENSIVE_QUERY))
        except OperationalError as exc:
            assert isinstance(exc.orig, sqlite3.OperationalError)
            interrupted.append(exc.orig.sqlite_errorcode)
            raise
        raise AssertionError("Unbounded query completed")

    with monkeypatch.context() as scoped:
        scoped.setattr(dashboard_reads, "_READ_BUDGET_SECONDS", 0.05)
        scoped.setattr(ApiKeysRepository, method, slow_read)
        response = await async_client.get(path)

    assert interrupted == [sqlite3.SQLITE_INTERRUPT]
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "dashboard_query_timeout"
    healthy = await async_client.get(path)
    assert healthy.status_code == 200
    if method == "list_usage_summary_by_key":
        assert [key["id"] for key in healthy.json()] == [key_id]
    else:
        assert healthy.json()["keyId"] == key_id


@pytest.mark.asyncio
async def test_api_key_mutations_keep_their_session_without_read_deadline(async_client, db_setup, monkeypatch):
    monkeypatch.setattr(dashboard_reads, "_READ_BUDGET_SECONDS", -1)
    created = await async_client.post("/api/api-keys", json={"name": "deadline-writer"})
    assert created.status_code == 200
    key_id = created.json()["id"]
    updated = await async_client.patch(f"/api/api-keys/{key_id}", json={"name": "updated-writer"})
    assert updated.status_code == 200
    async with SessionLocal() as session:
        assert await session.scalar(select(ApiKey.name).where(ApiKey.id == key_id)) == "updated-writer"
    regenerated = await async_client.post(f"/api/api-keys/{key_id}/regenerate")
    assert regenerated.status_code == 200
    assert regenerated.json()["key"] != created.json()["key"]
    deleted = await async_client.delete(f"/api/api-keys/{key_id}")
    assert deleted.status_code == 204
    async with SessionLocal() as session:
        assert await session.get(ApiKey, key_id) is None
