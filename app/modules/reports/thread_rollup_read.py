"""Exact retained-thread metrics from hourly caches and bounded raw edges."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import case, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DashboardServiceUnavailableError
from app.core.utils.time import utcnow
from app.db.models import (
    AccountUsageRollupState,
    ThreadIdentityAccountHour,
    ThreadIdentityDirtyHour,
    ThreadIdentityHour,
    ThreadIdentityPairHour,
)
from app.modules.accounts.usage_time_rollup import DIMENSION_SENTINEL, epoch_seconds
from app.modules.accounts.usage_time_rollup_read import ceil_to_grid, floor_to_grid
from app.modules.reports.thread_identity import (
    CONVERSATION_MIN_REQUESTS,
    ThreadIdentityFacetRow,
    _scoped,
    turn_condition,
)
from app.modules.reports.thread_rollup import hour_sources

MEASURES = (
    "requests",
    "unattributed_requests",
    "cache_input_tokens",
    "cache_cached_input_tokens",
    "turns",
    "account_switches",
    "conversations",
    "conversation_account_total",
    "single_account_conversations",
)


async def aggregate_cached_thread_identity(
    session: AsyncSession, start: datetime, end: datetime
) -> dict[bool, ThreadIdentityFacetRow]:
    lo, hi = ceil_to_grid(start, 3600), floor_to_grid(end, 3600)
    watermark = func.coalesce(
        select(AccountUsageRollupState.thread_folded_through).where(AccountUsageRollupState.id == 1).scalar_subquery(),
        literal(datetime(1970, 1, 1)),
    )
    high = case((watermark < hi, watermark), else_=literal(hi))
    tail = case((high <= lo, literal(start)), else_=high)
    dirty = (
        select(ThreadIdentityDirtyHour.bucket_epoch)
        .where(
            ThreadIdentityDirtyHour.bucket_epoch >= epoch_seconds(floor_to_grid(start, 3600)),
            ThreadIdentityDirtyHour.bucket_epoch < epoch_seconds(ceil_to_grid(end, 3600)),
        )
        .exists()
    )
    # A report ending at tomorrow's local midnight only has a live tail up
    # to now. A future empty period must not look like a stalled backfill.
    caught_up = tail >= min(end, utcnow()) - timedelta(hours=4)
    control = select(case((~caught_up, 1), (dirty, 2), else_=0).label("state")).cte("thread_coverage")
    ready = select(control.c.state).scalar_subquery() == 0
    # Put admission in the index bounds, not only an outer WHERE: SQLite can
    # otherwise evaluate/window the raw subquery before testing that guard.
    raw_tail = _scoped(session, case((ready, tail), else_=literal(end)), end)
    raw = select(*raw_tail.c).where(ready)
    if start < lo:
        leading = _scoped(session, case((ready & (high > lo), literal(start)), else_=literal(lo)), lo)
        raw = union_all(raw, select(*leading.c).where(ready, high > lo))
    raw_sources = hour_sources(raw.subquery())
    sources = []
    # The watermark is a statement constant; both representations are read
    # in the same snapshot as the dirty guard and account lifecycle changes.
    for table, raw_source in zip((ThreadIdentityHour, ThreadIdentityAccountHour, ThreadIdentityPairHour), raw_sources):
        folded = select(*table.__table__.c).where(
            table.bucket_epoch >= epoch_seconds(lo),
            table.bucket_epoch < epoch_seconds(hi),
            table.bucket_epoch
            < (
                func.extract("epoch", watermark)
                if session.get_bind().dialect.name == "postgresql"
                else func.strftime("%s", watermark).cast(ThreadIdentityHour.bucket_epoch.type)
            ),
            ready,
        )
        sources.append(union_all(folded, raw_source).cte())
    headers, accounts, pairs = sources

    def measures(keyed, **values):
        return [keyed.label("keyed"), *(values.get(name, literal(0)).label(name) for name in MEASURES)]

    request_totals = select(
        *measures(
            headers.c.keyed,
            requests=func.sum(headers.c.requests),
            cache_input_tokens=func.sum(headers.c.cache_input_tokens),
            cache_cached_input_tokens=func.sum(headers.c.cache_cached_input_tokens),
        )
    ).group_by(headers.c.keyed)
    unattributed = (
        select(*measures(accounts.c.keyed, unattributed_requests=func.sum(accounts.c.requests)))
        .where(accounts.c.account_id == DIMENSION_SENTINEL)
        .group_by(accounts.c.keyed)
    )
    threads = (
        select(headers.c.keyed, headers.c.thread_key, func.sum(headers.c.requests).label("requests"))
        .where(headers.c.thread_key != DIMENSION_SENTINEL)
        .group_by(headers.c.keyed, headers.c.thread_key)
        .cte()
    )
    presence = (
        select(
            accounts.c.keyed,
            accounts.c.thread_key,
            func.count(func.distinct(func.nullif(accounts.c.account_id, DIMENSION_SENTINEL))).label("accounts"),
        )
        .group_by(accounts.c.keyed, accounts.c.thread_key)
        .cte()
    )
    conversations = (
        select(
            *measures(
                threads.c.keyed,
                conversations=func.count(),
                conversation_account_total=func.sum(presence.c.accounts),
                single_account_conversations=func.sum(case((presence.c.accounts == 1, 1), else_=0)),
            )
        )
        .select_from(
            threads.join(
                presence, (threads.c.keyed == presence.c.keyed) & (threads.c.thread_key == presence.c.thread_key)
            )
        )
        .where(threads.c.requests >= CONVERSATION_MIN_REQUESTS)
        .group_by(threads.c.keyed)
    )
    internal = select(
        *measures(
            pairs.c.keyed,
            turns=func.sum(pairs.c.turns),
            account_switches=func.sum(case((pairs.c.from_account != pairs.c.to_account, pairs.c.turns), else_=0)),
        )
    ).group_by(pairs.c.keyed)
    partition = [headers.c.keyed, headers.c.thread_key]
    boundary = select(
        headers.c.keyed,
        func.nullif(headers.c.thread_key, DIMENSION_SENTINEL).label("thread_key"),
        headers.c.first_account.label("account_id"),
        headers.c.first_input.label("input_tokens"),
        headers.c.first_epoch.label("requested_epoch"),
        *(
            func.lag(getattr(headers.c, f"last_{source}"))
            .over(partition_by=partition, order_by=headers.c.bucket_epoch)
            .label(target)
            for source, target in (
                ("account", "previous_account_id"),
                ("input", "previous_input_tokens"),
                ("epoch", "previous_epoch"),
            )
        ),
    ).subquery()
    turn = turn_condition(boundary)
    boundaries = select(
        *measures(
            boundary.c.keyed,
            turns=func.sum(case((turn, 1), else_=0)),
            account_switches=func.sum(
                case((turn & (boundary.c.account_id != boundary.c.previous_account_id), 1), else_=0)
            ),
        )
    ).group_by(boundary.c.keyed)
    # Seed both facets so unavailable/empty queries still return their control
    # state without a separate preliminary SELECT racing a repair commit.
    totals = union_all(
        select(*measures(literal(0))),
        select(*measures(literal(1))),
        request_totals,
        unattributed,
        conversations,
        internal,
        boundaries,
    ).cte()
    stmt = (
        select(
            control.c.state,
            totals.c.keyed,
            *(func.coalesce(func.sum(getattr(totals.c, name)), 0).label(name) for name in MEASURES),
        )
        .select_from(control.join(totals, literal(True)))
        .group_by(control.c.state, totals.c.keyed)
    )
    rows = (await session.execute(stmt)).all()
    state = rows[0].state
    if state:
        raise DashboardServiceUnavailableError(
            "Thread identity history is being repaired. Retry after the background update."
            if state == 2
            else "Thread identity history is still being prepared. Retry after the background update.",
            code="thread_identity_repairing" if state == 2 else "thread_identity_building",
        )
    return {
        bool(row.keyed): ThreadIdentityFacetRow(
            keyed=bool(row.keyed), **{name: int(getattr(row, name)) for name in MEASURES}
        )
        for row in rows
    }
