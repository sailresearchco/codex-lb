"""Execution budgets for the private SQLite connections used by dashboard reads."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import asynccontextmanager, closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from math import ceil
from threading import RLock
from time import monotonic
from typing import Any, cast

import anyio
from aiosqlite import Connection as AsyncSQLiteConnection
from sqlalchemy import Connection, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DashboardServiceUnavailableError
from app.core.utils.shared_future import _await_cleanup_deferring_cancellation, wait_on_shared_future
from app.db.session import SessionLocal, close_session, engine, get_session
from app.db.sqlite_utils import sqlite_db_path_from_url

_READ_BUDGET_SECONDS = 15.0
_PROGRESS_STEPS = 1000


def _timeout_error() -> DashboardServiceUnavailableError:
    return DashboardServiceUnavailableError(
        "This query took too long. Narrow the time range or filters and retry.",
        code="dashboard_query_timeout",
    )


@dataclass(slots=True)
class _ReadBudget:
    expires_at: float
    cancelled: bool = False

    def remaining_ms(self) -> int:
        remaining = self.expires_at - monotonic()
        if remaining <= 0:
            raise _timeout_error()
        return max(1, ceil(remaining * 1000))

    def progress(self) -> int:
        # SQLite invokes this on its executing thread, including while rows
        # are fetched. Cancelling only the coroutine would leave that work running.
        return int(self.cancelled or monotonic() >= self.expires_at)

    def caused_timeout(self, error: BaseException) -> bool:
        original = error.orig if isinstance(error, OperationalError) else error
        code = getattr(original, "sqlite_errorcode", None)
        return (
            monotonic() >= self.expires_at
            and isinstance(code, int)
            and code & 0xFF in (sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        )


_read_budget: ContextVar[_ReadBudget | None] = ContextVar("dashboard_sqlite_read_budget", default=None)


async def run_sqlite_usage_read[T](function: Callable[..., T], *args: Any) -> T:
    budget = _read_budget.get()
    if budget is None:
        return await anyio.to_thread.run_sync(function, *args)
    worker = asyncio.create_task(anyio.to_thread.run_sync(function, *args))
    try:
        return await wait_on_shared_future(worker)
    except asyncio.CancelledError:
        budget.cancelled = True
        # Direct Task.cancel() can bypass AnyIO's thread shield. Retain the
        # worker until its query or bounded lock wait exits before releasing
        # the request's session and deadline context.
        try:
            await _await_cleanup_deferring_cancellation(worker)
        except Exception:
            # Cancellation owns the response; consume the interrupted worker's
            # failure rather than replace the caller's cancellation with it.
            pass
        raise


@contextmanager
def sqlite_usage_read_lock(lock: RLock) -> Iterator[None]:
    budget = _read_budget.get()
    if budget is None:
        with lock:
            yield
        return
    if not lock.acquire(timeout=budget.remaining_ms() / 1000):
        raise _timeout_error()
    try:
        budget.remaining_ms()
        yield
    finally:
        lock.release()


@asynccontextmanager
async def dashboard_read_budget(session: AsyncSession) -> AsyncGenerator[None]:
    bind = session.get_bind()
    # File SQLite checkouts are private. In-memory SQLite shares a connection
    # across sessions, so a connection-wide interrupt would affect other work.
    if bind.dialect.name != "sqlite" or sqlite_db_path_from_url(str(bind.engine.url)) is None:
        yield
        return

    connection = await session.connection()
    raw = await connection.get_raw_connection()
    driver = cast(AsyncSQLiteConnection, raw.driver_connection)
    async with driver.execute("PRAGMA busy_timeout") as cursor:
        row = await cursor.fetchone()
    original_busy_timeout = int(row[0]) if row else 30000
    budget = _ReadBudget(monotonic() + _READ_BUDGET_SECONDS)

    def before_statement(
        _connection: Connection, cursor: Any, _statement: str, _parameters: Any, _context: Any, _executemany: bool
    ) -> None:
        # Progress callbacks do not run while waiting for a lock. Each statement
        # gets only the remaining budget for that wait, including later queries.
        cursor.execute(f"PRAGMA busy_timeout={min(original_busy_timeout, budget.remaining_ms())}")

    token = _read_budget.set(budget)
    event.listen(connection.sync_connection, "before_cursor_execute", before_statement)
    try:
        await driver.set_progress_handler(budget.progress, _PROGRESS_STEPS)
        try:
            yield
        except (OperationalError, sqlite3.OperationalError) as exc:
            if budget.caused_timeout(exc):
                raise _timeout_error() from exc
            raise
        except asyncio.CancelledError:
            budget.cancelled = True
            # interrupt() calls sqlite3_interrupt directly, outside aiosqlite's
            # worker queue. The handler removal below then waits for owned work.
            with anyio.CancelScope(shield=True):
                if not connection.invalidated and not connection.closed:
                    await driver.interrupt()
            raise
    finally:
        budget.cancelled = True
        _read_budget.reset(token)
        event.remove(connection.sync_connection, "before_cursor_execute", before_statement)
        with anyio.CancelScope(shield=True):
            if not connection.invalidated and not connection.closed:
                # SQLite disables its progress handler when the interval is zero.
                await driver.set_progress_handler(lambda: 0, 0)
                async with driver.execute(f"PRAGMA busy_timeout={original_busy_timeout}"):
                    pass


async def get_dashboard_read_session() -> AsyncGenerator[AsyncSession]:
    if engine.dialect.name != "sqlite" or sqlite_db_path_from_url(str(engine.url)) is None:
        async with asynccontextmanager(get_session)() as session:
            yield session
        return

    # Keep the connection owned across repository commits, such as first-boot
    # settings creation, so later reads cannot escape the deadline and no
    # connection can return to a pool with its request's progress handler active.
    async with engine.connect() as connection:
        session = SessionLocal(bind=connection)
        try:
            async with dashboard_read_budget(session):
                yield session
        finally:
            await close_session(session)


class _BudgetedReadConnection(sqlite3.Connection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._budget = _read_budget.get()
        if self._budget is not None:
            self.set_progress_handler(self._budget.progress, _PROGRESS_STEPS)

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        if self._budget is not None:
            with closing(super().execute(f"PRAGMA busy_timeout={self._budget.remaining_ms()}")):
                pass
        return super().execute(sql, parameters)


@contextmanager
def sqlite_usage_read_connection(db_path: str) -> Iterator[sqlite3.Connection]:
    # AnyIO copies the request context into its worker. All three synchronous
    # usage readers share the request's deadline, including their fetchall calls.
    budget = _read_budget.get()
    factory = sqlite3.Connection if budget is None else _BudgetedReadConnection
    with closing(
        sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES, factory=factory)
    ) as connection:
        try:
            yield connection
        except sqlite3.OperationalError as exc:
            if budget is not None and budget.caused_timeout(exc):
                raise _timeout_error() from exc
            raise
