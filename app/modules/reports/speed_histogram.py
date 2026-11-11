"""Mergeable report speed distributions, folded without request-path backfills."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import ColumnElement, case, delete, func, insert, literal, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.time import utcnow
from app.db.models import AccountUsageRollupState, RequestLog, RequestReportSpeedHistogram
from app.db.session import get_background_session, sqlite_writer_section
from app.modules.accounts.usage_rollup import FOLD_LAG, _insert_fn, _locked_state, _state_bootstrap_stmt
from app.modules.accounts.usage_time_rollup import (
    _dimension_expr,
    _requested_at_epoch_bucket_expr,
    epoch_seconds,
    floor_to_hour,
    to_dimension,
)
from app.modules.reports.filters import _normal_traffic_clause

SpeedMetric = Literal["ttft", "tps", "queue"]
METRICS: tuple[SpeedMetric, ...] = ("ttft", "tps", "queue")
SPEED_FOLD_SLICE = timedelta(hours=1)
SPEED_MAX_SLICES = 8
KEYS = ("bucket_epoch", "account_id", "api_key_id", "model", "useragent_group", "metric", "bin_index")

# Fixed schema: changing these boundaries requires rebuilding the histogram.
# Timings are signed integer milliseconds. TPS is strictly positive.
_POSITIVE_TIMING_EDGES = tuple(2 ** (index / 8) for index in range(249))
TIMING_EDGES = (*(-value for value in reversed(_POSITIVE_TIMING_EDGES)), 0.0, *_POSITIVE_TIMING_EDGES)
TPS_EDGES = tuple(2 ** (index / 8) for index in range(-256, 249))
RELATIVE_ERROR_BOUND = (2 ** (1 / 8) - 1) / (2 ** (1 / 8) + 1)


@dataclass(frozen=True, slots=True)
class HistogramMedian:
    value: float
    sample_count: int
    lower_bound: float | None
    upper_bound: float | None
    relative_error_bound: float | None
    clipped: bool = False


def metric_value(metric: SpeedMetric) -> tuple[ColumnElement, ColumnElement]:
    if metric == "ttft":
        return RequestLog.latency_first_token_ms.expression, RequestLog.latency_first_token_ms.is_not(None)
    if metric == "queue":
        return RequestLog.latency_queue_ms.expression, RequestLog.latency_queue_ms.is_not(None)
    tokens = RequestLog.output_tokens - func.coalesce(RequestLog.reasoning_tokens, 0)
    duration = RequestLog.latency_ms - RequestLog.latency_first_token_ms
    return tokens * 1000.0 / duration, (tokens > 0) & (duration > 0)


def bin_expression(value: ColumnElement, metric: SpeedMetric) -> ColumnElement:
    """A balanced CASE tree needs at most nine comparisons per sample.

    Plain SQL keeps SQLite builds without optional math functions supported.
    Constants are internal numeric literals, never user-provided SQL.
    """
    edges = TPS_EDGES if metric == "tps" else TIMING_EDGES

    def branch(low: int, high: int) -> ColumnElement:
        if low == high:
            return literal_column(str(low))
        middle = (low + high) // 2
        return case(
            (value < literal_column(repr(edges[middle])), branch(low, middle)),
            else_=branch(middle + 1, high),
        )

    return branch(0, len(edges))


def histogram_median(metric: SpeedMetric, counts: dict[int, int]) -> HistogramMedian:
    total = sum(counts.values())
    if not total:
        return HistogramMedian(0.0, 0, None, None, None)
    edges = TPS_EDGES if metric == "tps" else TIMING_EDGES
    ranks = ((total + 1) // 2, total // 2 + 1)
    selected: list[int] = []
    cumulative = 0
    for index, count in sorted(counts.items()):
        cumulative += count
        while len(selected) < 2 and cumulative >= ranks[len(selected)]:
            selected.append(index)
    estimates: list[float] = []
    lower: list[float | None] = []
    upper: list[float | None] = []
    clipped = False
    for index in selected:
        lo = edges[index - 1] if index else None
        hi = edges[index] if index < len(edges) else None
        clipped |= lo is None or hi is None
        if metric != "tps" and lo in (-1.0, 0.0):
            # These half-open bins contain one integer timing value each.
            hi = lo
        if lo is None:
            assert hi is not None
            estimate = hi
        elif hi is None:
            estimate = lo
        else:
            estimate = 2 * lo * hi / (lo + hi) if lo + hi else 0.0
        estimates.append(estimate)
        lower.append(lo)
        upper.append(hi)
    lower_bound = sum(value for value in lower if value is not None) / 2 if all(v is not None for v in lower) else None
    upper_bound = sum(value for value in upper if value is not None) / 2 if all(v is not None for v in upper) else None
    # Opposite-sign central values can cancel, so only their absolute bounds
    # are meaningful; no relative guarantee is claimed across zero or overflow.
    relative = RELATIVE_ERROR_BOUND if not clipped and estimates[0] * estimates[1] >= 0 else None
    return HistogramMedian(sum(estimates) / 2, total, lower_bound, upper_bound, relative, clipped)


def histogram_fold_insert(session: AsyncSession, metric: SpeedMetric, start: datetime, end: datetime):
    value, valid = metric_value(metric)
    dimensions = [
        _requested_at_epoch_bucket_expr(session, 3600),
        _dimension_expr(RequestLog.account_id),
        _dimension_expr(RequestLog.api_key_id),
        RequestLog.model,
        _dimension_expr(RequestLog.useragent_group),
        literal(metric),
        bin_expression(value, metric),
    ]
    source = (
        select(*dimensions, func.count())
        .where(RequestLog.requested_at >= start, RequestLog.requested_at < end, _normal_traffic_clause(), valid)
        .group_by(*dimensions[:5], dimensions[6])
    )
    return insert(RequestReportSpeedHistogram).from_select([*KEYS, "sample_count"], source)


async def fold_next_speed_histogram_slice(session: AsyncSession, target: datetime) -> bool:
    async with sqlite_writer_section():
        state = await _locked_state(session)
        if state is None:
            await session.execute(_state_bootstrap_stmt(session))
            await session.commit()
            state = await _locked_state(session)
        if state is None or state.speed_folded_through >= target:
            return False
        watermark = state.speed_folded_through
        next_at = await session.scalar(
            select(func.min(RequestLog.requested_at)).where(
                RequestLog.requested_at >= watermark, RequestLog.requested_at < target
            )
        )
        end = target
        if next_at is not None:
            start = max(watermark, floor_to_hour(next_at))
            end = min(start + SPEED_FOLD_SLICE, target)
            await session.execute(
                delete(RequestReportSpeedHistogram).where(
                    RequestReportSpeedHistogram.bucket_epoch >= epoch_seconds(start),
                    RequestReportSpeedHistogram.bucket_epoch < epoch_seconds(end),
                )
            )
            for metric in METRICS:
                await session.execute(histogram_fold_insert(session, metric, start, end))
        await session.execute(
            update(AccountUsageRollupState)
            .where(AccountUsageRollupState.id == 1)
            .values(speed_folded_through=end, speed_history_from=state.speed_history_from or next_at or target)
        )
        await session.commit()
        return True


async def run_speed_histogram_fold_pass(*, now: datetime | None = None) -> int:
    target = floor_to_hour((now or utcnow()) - FOLD_LAG)
    committed = 0
    for _ in range(SPEED_MAX_SLICES):
        async with get_background_session() as session:
            if not await fold_next_speed_histogram_slice(session, target):
                break
            committed += 1
    return committed


async def fold_late_speed_log(session: AsyncSession, log: RequestLog) -> None:
    """Mirror a backdated append; caller holds the fold lock before INSERT.

    Existing coverage does not move backwards: one newly arrived sample
    cannot restore history pruned before histogram installation.
    """
    state = await _locked_state(session)
    if state is None or log.requested_at >= state.speed_folded_through:
        return
    if log.source == "limit_warmup" or log.request_kind in ("warmup", "limit_warmup"):
        return
    tps = None
    if log.output_tokens is not None and log.latency_ms is not None and log.latency_first_token_ms is not None:
        tokens = log.output_tokens - (log.reasoning_tokens or 0)
        duration = log.latency_ms - log.latency_first_token_ms
        if tokens > 0 and duration > 0:
            tps = tokens * 1000.0 / duration
    for metric, value in zip(METRICS, (log.latency_first_token_ms, tps, log.latency_queue_ms), strict=True):
        if value is None:
            continue
        edges = TPS_EDGES if metric == "tps" else TIMING_EDGES
        upsert = _insert_fn(session)(RequestReportSpeedHistogram).values(
            bucket_epoch=epoch_seconds(log.requested_at) // 3600 * 3600,
            account_id=to_dimension(log.account_id),
            api_key_id=to_dimension(log.api_key_id),
            model=log.model,
            useragent_group=to_dimension(log.useragent_group),
            metric=metric,
            bin_index=bisect_right(edges, value),
            sample_count=1,
        )
        await session.execute(
            upsert.on_conflict_do_update(
                index_elements=list(KEYS),
                set_={"sample_count": RequestReportSpeedHistogram.sample_count + 1},
            )
        )


async def rekey_speed_histogram_accounts(session: AsyncSession, account_ids: list[str], target_id: str | None) -> None:
    table = RequestReportSpeedHistogram
    source = table.account_id.in_([to_dimension(value) for value in account_ids])
    target = to_dimension(target_id)
    keys = [getattr(table, name) for name in KEYS if name != "account_id"]
    columns = [literal(target) if name == "account_id" else getattr(table, name) for name in KEYS]
    rows = select(*columns, func.sum(table.sample_count)).where(source).group_by(*keys)
    upsert = _insert_fn(session)(table).from_select([*KEYS, "sample_count"], rows)
    await session.execute(
        upsert.on_conflict_do_update(
            index_elements=list(KEYS), set_={"sample_count": table.sample_count + upsert.excluded.sample_count}
        )
    )
    await session.execute(delete(table).where(source))


async def delete_speed_histogram_account(session: AsyncSession, account_id: str) -> None:
    await session.execute(
        delete(RequestReportSpeedHistogram).where(RequestReportSpeedHistogram.account_id == to_dimension(account_id))
    )
