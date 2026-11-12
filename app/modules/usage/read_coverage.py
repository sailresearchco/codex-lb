"""Reject populated historical raw ranges before dashboard aggregation."""

from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DashboardServiceUnavailableError
from app.db.models import RequestLog
from app.db.request_log_predicates import countable_account_usage_clause, countable_api_key_usage_clause

MAX_RAW_TAIL = timedelta(hours=4)


def _building(code: str) -> DashboardServiceUnavailableError:
    return DashboardServiceUnavailableError("Historical usage is still being aggregated. Retry shortly.", code=code)


async def require_bounded_account_history(
    session: AsyncSession,
    watermark: datetime | None,
    cutoff: datetime,
) -> None:
    if watermark is not None and watermark >= cutoff:
        return
    times = [RequestLog.requested_at < cutoff]
    if watermark is not None:
        times.append(RequestLog.requested_at > watermark)
    eligible = select(RequestLog.id).where(countable_account_usage_clause(RequestLog.__table__), *times)
    if await session.scalar(select(eligible.exists())):
        raise _building("usage_history_building")


async def require_bounded_raw_history(
    session: AsyncSession,
    ranges: list[tuple[datetime | None, datetime]],
    *,
    code: Literal["usage_history_building", "report_history_building"],
) -> None:
    """Probe excess ranges with index bounds, without aggregating or filtering dimensions.

    These are physical-work checks: arbitrary dimension predicates could make an
    empty-match probe scan the very history it is meant to avoid. Callers retain
    their original aggregate bounds so a late insert after this check is counted.
    """
    probes = []
    for start, end in ranges:
        if start is not None and start >= end:
            continue
        query = select(RequestLog.id).where(RequestLog.requested_at < end)
        if start is not None:
            query = query.where(RequestLog.requested_at >= start)
        probes.append(query.exists())
    if probes and await session.scalar(select(or_(*probes))):
        raise _building(code)


async def require_bounded_api_key_history(
    session: AsyncSession,
    watermark: datetime | None,
    cutoff: datetime,
) -> None:
    if watermark is not None and watermark >= cutoff:
        return
    times = [RequestLog.requested_at < cutoff]
    if watermark is not None:
        times.append(RequestLog.requested_at > watermark)
    eligible = select(RequestLog.id).where(countable_api_key_usage_clause(RequestLog.__table__), *times)
    if await session.scalar(select(eligible.exists())):
        raise _building("usage_history_building")
