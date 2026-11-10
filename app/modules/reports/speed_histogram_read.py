"""Single-snapshot histogram reads with bounded raw complements."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, cast

from sqlalchemy import BigInteger, case, exists, func, literal, select, true, union_all
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.time import utcnow
from app.db.models import AccountUsageRollupState, RequestLog, RequestReportSpeedHistogram
from app.modules.accounts.usage_time_rollup import epoch_seconds, to_dimension
from app.modules.accounts.usage_time_rollup_read import ceil_to_grid, floor_to_grid
from app.modules.reports.filters import MISSING_USERAGENT_GROUP, _normal_traffic_clause
from app.modules.reports.speed_histogram import (
    METRICS,
    HistogramMedian,
    SpeedMetric,
    bin_expression,
    histogram_median,
    metric_value,
)

MAX_RAW_TAIL = timedelta(hours=4)
_EPOCH = datetime(1970, 1, 1)


@dataclass(frozen=True, slots=True)
class DailySpeedHistogram:
    date: str
    ttft: HistogramMedian
    tps: HistogramMedian
    queue: HistogramMedian


@dataclass(frozen=True, slots=True)
class SpeedHistogramResult:
    available: bool
    reason: Literal["building", "raw_history_pruned"] | None
    coverage_from: datetime | None
    folded_through: datetime
    daily: list[DailySpeedHistogram]


def speed_histogram_stmt(
    session: AsyncSession,
    windows: list[tuple[str, datetime, datetime]],
    account_ids: list[str] | None = None,
    model: str | None = None,
    useragent_group: str | None = None,
    api_key_ids: list[str] | None = None,
    *,
    now: datetime | None = None,
):
    if not windows or len(windows) > 7:
        raise ValueError("Speed reports require between one and seven daily windows")
    h = RequestReportSpeedHistogram
    observed_at = now or utcnow()
    folded_filters = []
    raw_filters = [_normal_traffic_clause()]
    for values, folded_column, raw_column in (
        (account_ids, h.account_id, RequestLog.account_id),
        (api_key_ids, h.api_key_id, RequestLog.api_key_id),
    ):
        if values:
            folded_filters.append(folded_column.in_([to_dimension(value) for value in values]))
            raw_filters.append(raw_column.in_(values))
    if model:
        folded_filters.append(h.model == model)
        raw_filters.append(RequestLog.model == model)
    if useragent_group:
        group = None if useragent_group == MISSING_USERAGENT_GROUP else useragent_group
        folded_filters.append(h.useragent_group == to_dimension(group))
        raw_filters.append(RequestLog.useragent_group == group)

    state = select(
        func.coalesce(
            select(AccountUsageRollupState.speed_folded_through)
            .where(AccountUsageRollupState.id == 1)
            .scalar_subquery(),
            literal(_EPOCH),
        ).label("watermark"),
        select(AccountUsageRollupState.speed_history_from)
        .where(AccountUsageRollupState.id == 1)
        .scalar_subquery()
        .label("coverage_from"),
        select(AccountUsageRollupState.request_logs_pruned_through)
        .where(AccountUsageRollupState.id == 1)
        .scalar_subquery()
        .label("pruned_through"),
        select(func.min(RequestLog.requested_at)).scalar_subquery().label("earliest_raw"),
    ).cte("speed_state")
    needs_history = any(start < observed_at for _, start, _ in windows)
    building = (state.c.watermark <= _EPOCH) | state.c.coverage_from.is_(None) if needs_history else literal(False)
    missing_edge = literal(False)
    for _, start, end in windows:
        # Only the unfolded live tail may use raw data. A cold or lagged
        # watermark never turns an endpoint into a historical backfill.
        required_end = min(end, observed_at)
        if required_end - start > MAX_RAW_TAIL:
            building |= state.c.watermark < required_end - MAX_RAW_TAIL
        if start < required_end:
            missing_edge |= state.c.coverage_from > start
        for boundary, partial in (
            (start, start != floor_to_grid(start, 3600)),
            (floor_to_grid(end, 3600), end != floor_to_grid(end, 3600)),
        ):
            if not partial:
                continue
            bucket = epoch_seconds(floor_to_grid(boundary, 3600))
            folded_samples = exists(select(1).where(h.bucket_epoch == bucket, *folded_filters))
            missing_edge |= (
                (state.c.watermark > boundary)
                & (
                    (state.c.pruned_through.is_not(None) & (state.c.pruned_through >= boundary))
                    | state.c.earliest_raw.is_(None)
                    | (state.c.earliest_raw > boundary)
                )
                & folded_samples
            )
    status = select(
        state.c.watermark,
        state.c.coverage_from,
        case((building, 0), (missing_edge, 1), else_=2).label("availability"),
    ).cte("speed_coverage")
    parts = [
        select(
            literal(label).label("report_date"),
            literal(start).label("start_at"),
            literal(end).label("end_at"),
            literal(ceil_to_grid(start, 3600)).label("lo_at"),
            literal(floor_to_grid(end, 3600)).label("hi_at"),
            literal(epoch_seconds(ceil_to_grid(start, 3600))).label("lo_epoch"),
            literal(epoch_seconds(floor_to_grid(end, 3600))).label("hi_epoch"),
        )
        for label, start, end in windows
    ]
    w = (parts[0] if len(parts) == 1 else union_all(*parts)).cte("speed_windows")
    watermark_epoch = (
        sql_cast(func.extract("epoch", status.c.watermark), BigInteger)
        if session.get_bind().dialect.name == "postgresql"
        else sql_cast(func.strftime("%s", status.c.watermark), BigInteger)
    )
    folded = (
        select(w.c.report_date, h.metric, h.bin_index, func.sum(h.sample_count).label("sample_count"))
        .select_from(
            w.join(status, true()).join(
                h,
                (h.bucket_epoch >= w.c.lo_epoch) & (h.bucket_epoch < w.c.hi_epoch) & (h.bucket_epoch < watermark_epoch),
            )
        )
        .where(status.c.availability == 2, *folded_filters)
        .group_by(w.c.report_date, h.metric, h.bin_index)
    )
    high = case((status.c.watermark < w.c.hi_at, status.c.watermark), else_=w.c.hi_at)
    tail_start = case((high <= w.c.lo_at, w.c.start_at), else_=high)
    branches = [folded]
    for metric in METRICS:
        value, valid = metric_value(metric)
        bin_index = bin_expression(value, metric)

        def raw(start, end, *extra):
            return (
                select(
                    w.c.report_date,
                    literal(metric).label("metric"),
                    bin_index.label("bin_index"),
                    func.count().label("sample_count"),
                )
                .select_from(
                    w.join(status, true()).join(
                        RequestLog, (RequestLog.requested_at >= start) & (RequestLog.requested_at < end)
                    )
                )
                .where(status.c.availability == 2, *raw_filters, valid, *extra)
                .group_by(w.c.report_date, bin_index)
            )

        branches.append(raw(tail_start, w.c.end_at))
        if any(start != ceil_to_grid(start, 3600) for _, start, _ in windows):
            branches.append(raw(w.c.start_at, w.c.lo_at, high > w.c.lo_at))
    source = union_all(*branches).subquery("speed_source")
    counts = (
        select(
            source.c.report_date,
            source.c.metric,
            source.c.bin_index,
            func.sum(source.c.sample_count).label("sample_count"),
        )
        .group_by(source.c.report_date, source.c.metric, source.c.bin_index)
        .subquery("speed_counts")
    )
    return select(status.c.availability, status.c.watermark, status.c.coverage_from, counts).select_from(
        status.outerjoin(counts, true())
    )


async def read_speed_histograms(
    session: AsyncSession,
    windows: list[tuple[str, datetime, datetime]],
    account_ids: list[str] | None = None,
    model: str | None = None,
    useragent_group: str | None = None,
    api_key_ids: list[str] | None = None,
    *,
    now: datetime | None = None,
) -> SpeedHistogramResult:
    rows = (
        await session.execute(
            speed_histogram_stmt(session, windows, account_ids, model, useragent_group, api_key_ids, now=now)
        )
    ).all()
    availability, watermark = rows[0].availability, rows[0].watermark
    if availability != 2:
        return SpeedHistogramResult(
            False, "building" if availability == 0 else "raw_history_pruned", rows[0].coverage_from, watermark, []
        )
    counts: dict[tuple[str, SpeedMetric], dict[int, int]] = {}
    for row in rows:
        if row.report_date is not None:
            counts.setdefault((row.report_date, cast(SpeedMetric, row.metric)), {})[row.bin_index] = int(
                row.sample_count
            )
    daily = [
        DailySpeedHistogram(label, *(histogram_median(metric, counts.get((label, metric), {})) for metric in METRICS))
        for label, _, _ in windows
    ]
    return SpeedHistogramResult(True, None, rows[0].coverage_from, watermark, daily)
