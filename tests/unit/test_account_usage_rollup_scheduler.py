from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock

import pytest

import app.modules.accounts.usage_rollup_scheduler as rollup_scheduler
from app.modules.accounts.usage_rollup_scheduler import AccountUsageRollupScheduler

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def report_fold(monkeypatch):
    fold = AsyncMock(return_value=0)
    monkeypatch.setattr(rollup_scheduler, "run_report_fold_pass", fold)
    monkeypatch.setattr(rollup_scheduler, "run_conversation_fold_pass", AsyncMock(return_value=0))
    monkeypatch.setattr(rollup_scheduler, "run_speed_histogram_fold_pass", AsyncMock(return_value=0))
    monkeypatch.setattr(rollup_scheduler, "run_thread_fold_pass", AsyncMock(return_value=0))
    monkeypatch.setattr(rollup_scheduler, "run_conversation_summary_pass", AsyncMock(return_value=0))
    return fold


class _GateLeader:
    """Leader stub mirroring ``run_if_leader``: heartbeat gate, not one-shot.

    A one-time ``try_acquire`` would leave a fold backfill that outlives the
    60s lease unprotected; ``try_acquire`` therefore asserts if the scheduler
    ever falls back to it.
    """

    def __init__(self, *, leader: bool) -> None:
        self.leader = leader
        self.run_if_leader_calls = 0

    async def run_if_leader(self, fn: Callable[[], Awaitable[object]]) -> object | None:
        self.run_if_leader_calls += 1
        if not self.leader:
            return None
        return await fn()

    async def try_acquire(self) -> bool:  # pragma: no cover - must not be used
        raise AssertionError("usage rollup scheduler must gate via run_if_leader, not try_acquire")


def test_build_account_usage_rollup_scheduler_uses_constant_interval() -> None:
    scheduler = rollup_scheduler.build_account_usage_rollup_scheduler()
    assert scheduler.interval_seconds == rollup_scheduler.FOLD_INTERVAL_SECONDS


@pytest.mark.asyncio
async def test_fold_once_skips_when_not_leader(monkeypatch, report_fold) -> None:
    leader = _GateLeader(leader=False)
    monkeypatch.setattr(rollup_scheduler, "_get_leader_election", lambda: leader)
    fold = AsyncMock()
    hourly = AsyncMock()
    monkeypatch.setattr(rollup_scheduler, "run_fold_pass", fold)
    monkeypatch.setattr(rollup_scheduler, "run_hourly_fold_pass", hourly)

    await AccountUsageRollupScheduler(interval_seconds=1)._fold_once()

    report_fold.assert_not_awaited()
    fold.assert_not_called()
    hourly.assert_not_called()
    assert leader.run_if_leader_calls == 1


@pytest.mark.asyncio
async def test_fold_once_gates_via_run_if_leader_heartbeat(monkeypatch) -> None:
    """The fold pass must run under the heartbeat-renewed ``run_if_leader``
    gate, not a one-time ``try_acquire`` that leaves a long backfill
    unprotected once the lease expires mid-pass."""
    leader = _GateLeader(leader=True)
    monkeypatch.setattr(rollup_scheduler, "_get_leader_election", lambda: leader)
    fold = AsyncMock(return_value=2)
    hourly = AsyncMock(return_value=1)
    monkeypatch.setattr(rollup_scheduler, "run_fold_pass", fold)
    monkeypatch.setattr(rollup_scheduler, "run_hourly_fold_pass", hourly)

    await AccountUsageRollupScheduler(interval_seconds=1)._fold_once()

    fold.assert_awaited_once()
    hourly.assert_awaited_once()
    assert leader.run_if_leader_calls == 1


@pytest.mark.asyncio
async def test_fold_once_swallows_fold_errors(monkeypatch) -> None:
    leader = _GateLeader(leader=True)
    monkeypatch.setattr(rollup_scheduler, "_get_leader_election", lambda: leader)
    fold = AsyncMock(side_effect=RuntimeError("db down"))
    hourly = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(rollup_scheduler, "run_fold_pass", fold)
    monkeypatch.setattr(rollup_scheduler, "run_hourly_fold_pass", hourly)

    await AccountUsageRollupScheduler(interval_seconds=1)._fold_once()

    fold.assert_awaited_once()
    hourly.assert_awaited_once()


@pytest.mark.asyncio
async def test_fold_once_runs_hourly_pass_even_when_lifetime_pass_fails(monkeypatch, report_fold) -> None:
    """Blast-radius isolation: a lifetime-fold failure must not stop the
    hourly time-axis fold (and vice versa) — each has its own watermark and
    retention only pauses via the min-gate."""
    leader = _GateLeader(leader=True)
    monkeypatch.setattr(rollup_scheduler, "_get_leader_election", lambda: leader)
    fold = AsyncMock(side_effect=RuntimeError("lifetime fold broken"))
    hourly = AsyncMock(return_value=3)
    monkeypatch.setattr(rollup_scheduler, "run_fold_pass", fold)
    monkeypatch.setattr(rollup_scheduler, "run_hourly_fold_pass", hourly)

    await AccountUsageRollupScheduler(interval_seconds=1)._fold_once()

    fold.assert_awaited_once()
    hourly.assert_awaited_once()
    report_fold.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_folds_immediately_and_stop_cancels(monkeypatch) -> None:
    leader = _GateLeader(leader=True)
    monkeypatch.setattr(rollup_scheduler, "_get_leader_election", lambda: leader)
    folded = asyncio.Event()

    async def _fold(**_kwargs):
        folded.set()
        return 0

    monkeypatch.setattr(rollup_scheduler, "run_fold_pass", _fold)
    monkeypatch.setattr(rollup_scheduler, "run_hourly_fold_pass", AsyncMock(return_value=0))

    scheduler = AccountUsageRollupScheduler(interval_seconds=3600)
    await scheduler.start()
    await asyncio.wait_for(folded.wait(), timeout=5)
    await scheduler.stop()
    assert scheduler._task is None
    assert scheduler._summary_task is None
    assert scheduler._backfill_tasks == ()


@pytest.mark.asyncio
async def test_stop_drains_owned_summary_backfill(monkeypatch):
    leader = _GateLeader(leader=True)
    monkeypatch.setattr(rollup_scheduler, "_get_leader_election", lambda: leader)
    entered = asyncio.Event()
    exited = asyncio.Event()

    async def backfill():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    monkeypatch.setattr(rollup_scheduler, "run_conversation_summary_pass", backfill)
    scheduler = AccountUsageRollupScheduler(interval_seconds=3600)
    await scheduler.start()
    await asyncio.wait_for(entered.wait(), timeout=5)
    await scheduler.stop()
    assert exited.is_set()
    assert scheduler._task is None
    assert scheduler._summary_task is None
    assert scheduler._backfill_tasks == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("fold_name", ["run_speed_histogram_fold_pass", "run_thread_fold_pass"])
async def test_bounded_backfill_continues_without_repeating_other_folds(monkeypatch, fold_name):
    leader = _GateLeader(leader=True)
    monkeypatch.setattr(rollup_scheduler, "_get_leader_election", lambda: leader)
    monkeypatch.setattr(rollup_scheduler, "BACKFILL_INTERVAL_SECONDS", 0.001)
    caught_up = asyncio.Event()
    calls = 0

    async def backfill():
        nonlocal calls
        calls += 1
        if calls == 3:
            caught_up.set()
            return 1
        return 8

    lifetime = AsyncMock(return_value=0)
    hourly = AsyncMock(return_value=0)
    other_name = (
        "run_thread_fold_pass" if fold_name == "run_speed_histogram_fold_pass" else "run_speed_histogram_fold_pass"
    )
    failed_fold = AsyncMock(side_effect=RuntimeError("independent fold failed"))
    monkeypatch.setattr(rollup_scheduler, "run_fold_pass", lifetime)
    monkeypatch.setattr(rollup_scheduler, "run_hourly_fold_pass", hourly)
    monkeypatch.setattr(rollup_scheduler, fold_name, backfill)
    monkeypatch.setattr(rollup_scheduler, other_name, failed_fold)
    scheduler = AccountUsageRollupScheduler(interval_seconds=3600)
    await scheduler.start()
    try:
        await asyncio.wait_for(caught_up.wait(), timeout=5)
        assert calls == 3
        lifetime.assert_awaited_once()
        hourly.assert_awaited_once()
        failed_fold.assert_awaited_once()
    finally:
        await scheduler.stop()
    assert scheduler._backfill_tasks == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [0, 1, 8, None])
async def test_bounded_backfill_waits_for_progress_and_leadership(monkeypatch, committed):
    scheduler = AccountUsageRollupScheduler(interval_seconds=900)
    leader = _GateLeader(leader=committed is not None)
    monkeypatch.setattr(rollup_scheduler, "_get_leader_election", lambda: leader)
    fold = AsyncMock(return_value=committed)
    waits = []

    async def wait_once(awaitable, *, timeout):
        waits.append(timeout)
        scheduler._stop.set()
        await awaitable

    monkeypatch.setattr(rollup_scheduler.asyncio, "wait_for", wait_once)
    await scheduler._run_bounded_fold_loop(fold, 8)
    assert waits == [rollup_scheduler.BACKFILL_INTERVAL_SECONDS if committed == 8 else 900]
    assert fold.await_count == (0 if committed is None else 1)
    assert leader.run_if_leader_calls == 1


@pytest.mark.asyncio
async def test_stop_cancels_active_and_queued_backfills_and_restart_owns_new_tasks(monkeypatch):
    leader = _GateLeader(leader=True)
    monkeypatch.setattr(rollup_scheduler, "_get_leader_election", lambda: leader)
    monkeypatch.setattr(rollup_scheduler, "run_fold_pass", AsyncMock(return_value=0))
    monkeypatch.setattr(rollup_scheduler, "run_hourly_fold_pass", AsyncMock(return_value=0))
    entered = asyncio.Event()
    exited = asyncio.Event()

    async def blocked_backfill():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()
        return 0

    monkeypatch.setattr(rollup_scheduler, "run_speed_histogram_fold_pass", blocked_backfill)
    thread = AsyncMock(return_value=0)
    monkeypatch.setattr(rollup_scheduler, "run_thread_fold_pass", thread)
    scheduler = AccountUsageRollupScheduler(interval_seconds=3600)
    await scheduler.start()
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        owned = scheduler._backfill_tasks
        await scheduler.start()
        assert scheduler._backfill_tasks == owned
        thread.assert_not_awaited()
    finally:
        await scheduler.stop()
    assert exited.is_set()
    assert all(task.done() for task in owned)
    assert not scheduler._lock.locked()
    assert scheduler._backfill_tasks == ()
    entered.clear()
    await scheduler.start()
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert scheduler._backfill_tasks != owned
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_stop_drains_backfills_when_periodic_loop_already_failed(monkeypatch):
    leader = _GateLeader(leader=True)
    monkeypatch.setattr(rollup_scheduler, "_get_leader_election", lambda: leader)
    entered = asyncio.Event()
    exited = asyncio.Event()

    async def failed_loop(self):
        raise RuntimeError("periodic loop failed")

    async def blocked_backfill():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()
        return 0

    monkeypatch.setattr(AccountUsageRollupScheduler, "_run_loop", failed_loop)
    monkeypatch.setattr(rollup_scheduler, "run_speed_histogram_fold_pass", blocked_backfill)
    scheduler = AccountUsageRollupScheduler(interval_seconds=3600)
    await scheduler.start()
    await asyncio.wait_for(entered.wait(), timeout=5)
    owned = scheduler._backfill_tasks
    with pytest.raises(RuntimeError, match="periodic loop failed"):
        await scheduler.stop()
    assert exited.is_set()
    assert all(task.done() for task in owned)
    assert not scheduler._lock.locked()
    assert scheduler._task is None
    assert scheduler._summary_task is None
    assert scheduler._backfill_tasks == ()
    await scheduler.stop()
