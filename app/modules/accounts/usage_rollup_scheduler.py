from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.scheduling.leader_election_handle import get_leader_election as _get_leader_election
from app.modules.accounts.usage_rollup import run_fold_pass
from app.modules.accounts.usage_time_rollup import run_conversation_fold_pass, run_hourly_fold_pass
from app.modules.reports.rollup import run_report_fold_pass
from app.modules.reports.speed_histogram import SPEED_MAX_SLICES, run_speed_histogram_fold_pass
from app.modules.reports.thread_rollup import MAX_SLICES as THREAD_MAX_SLICES
from app.modules.reports.thread_rollup import run_thread_fold_pass
from app.modules.request_logs.conversation_summary import run_conversation_summary_pass

logger = logging.getLogger(__name__)

FOLD_INTERVAL_SECONDS = 900
BACKFILL_INTERVAL_SECONDS = 1


@dataclass(slots=True)
class AccountUsageRollupScheduler:
    interval_seconds: int
    _task: asyncio.Task[None] | None = None
    _summary_task: asyncio.Task[None] | None = None
    _backfill_tasks: tuple[asyncio.Task[None], ...] = ()
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        if self._task is not None or self._summary_task is not None or self._backfill_tasks:
            await self.stop()
        self._stop.clear()
        self._summary_task = asyncio.create_task(self._run_summary_loop())
        self._task = asyncio.create_task(self._run_loop())
        self._backfill_tasks = (
            asyncio.create_task(self._run_bounded_fold_loop(run_speed_histogram_fold_pass, SPEED_MAX_SLICES)),
            asyncio.create_task(self._run_bounded_fold_loop(run_thread_fold_pass, THREAD_MAX_SLICES)),
        )

    async def stop(self) -> None:
        self._stop.set()
        tasks = [task for task in (self._task, self._summary_task, *self._backfill_tasks) if task is not None]
        for task in tasks:
            task.cancel()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        self._summary_task = None
        self._backfill_tasks = ()
        self._task = None
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
                raise outcome

    async def _run_bounded_fold_loop(self, fold: Callable[[], Awaitable[int]], max_slices: int) -> None:
        while not self._stop.is_set():
            committed = 0
            try:
                committed = await _get_leader_election().run_if_leader(lambda: self._bounded_fold_as_leader(fold)) or 0
            except Exception:
                logger.exception("Bounded usage backfill pass failed")
            try:
                # A full pass may have more history. Release the shared lock
                # and yield before continuing; failures and caught-up folds
                # retain their normal cadence independently of other folds.
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=BACKFILL_INTERVAL_SECONDS if committed == max_slices else self.interval_seconds,
                )
            except asyncio.TimeoutError:
                continue

    async def _bounded_fold_as_leader(self, fold: Callable[[], Awaitable[int]]) -> int:
        async with self._lock:
            return await fold()

    async def _run_summary_loop(self) -> None:
        while not self._stop.is_set():
            processed = 0
            try:
                processed = await _get_leader_election().run_if_leader(self._summary_as_leader) or 0
            except Exception:
                logger.exception("Conversation summary backfill failed")
            try:
                # Backlog progresses without waiting for the 15-minute usage
                # fold tick. Each pass and transaction remains bounded.
                await asyncio.wait_for(self._stop.wait(), timeout=1 if processed else 60)
            except asyncio.TimeoutError:
                continue

    async def _summary_as_leader(self) -> int:
        async with self._lock:
            return await run_conversation_summary_pass()

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            await self._fold_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def _fold_once(self) -> None:
        await _get_leader_election().run_if_leader(self._fold_as_leader)

    async def _fold_as_leader(self) -> None:
        async with self._lock:
            try:
                await run_fold_pass()
            except Exception:
                logger.exception("Account usage rollup fold pass failed")
            # Separate try-blocks on purpose: a bug in any one fold must not
            # stop the others — retention only ever pauses, because its prune
            # gate takes the MIN of every watermark.
            try:
                await run_hourly_fold_pass()
            except Exception:
                logger.exception("Hourly usage rollup fold pass failed")
            try:
                await run_conversation_fold_pass()
            except Exception:
                logger.exception("Conversation rollup fold pass failed")
            try:
                await run_report_fold_pass()
            except Exception:
                logger.exception("Report rollup fold pass failed")


def build_account_usage_rollup_scheduler() -> AccountUsageRollupScheduler:
    return AccountUsageRollupScheduler(interval_seconds=FOLD_INTERVAL_SECONDS)
