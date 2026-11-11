"""Disposable hourly thread-identity caches and atomic repair markers."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import case, delete, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.time import utcnow
from app.db.models import (
    AccountUsageRollupState,
    RequestLog,
    ThreadIdentityAccountHour,
    ThreadIdentityDirtyHour,
    ThreadIdentityHour,
    ThreadIdentityPairHour,
)
from app.db.session import get_background_session, sqlite_writer_section
from app.modules.accounts.usage_rollup import FOLD_LAG, _insert_fn, _locked_state, _state_bootstrap_stmt
from app.modules.accounts.usage_time_rollup import (
    _dimension_expr,
    _requested_at_epoch_bucket_expr,
    epoch_seconds,
    floor_to_hour,
    to_dimension,
)
from app.modules.accounts.usage_time_rollup_read import epoch_to_datetime
from app.modules.reports.thread_identity import CACHE_MIN_INPUT_TOKENS, _scoped, turn_condition

HOUR = timedelta(hours=1)
MAX_SLICES = 8
KEYS = ("bucket_epoch", "keyed", "thread_key")
TABLES = (ThreadIdentityHour, ThreadIdentityAccountHour, ThreadIdentityPairHour)


def hour_sources(scoped):
    """Three SQL projections; ordering stays local to each thread and hour."""
    bucket = scoped.c.requested_hour
    partition = [bucket, scoped.c.keyed, scoped.c.thread_key]
    order = [scoped.c.requested_at, scoped.c.id]
    ordered = select(
        *scoped.c,
        bucket.label("bucket_epoch"),
        func.row_number().over(partition_by=partition, order_by=order).label("first_rank"),
        func.row_number().over(partition_by=partition, order_by=[column.desc() for column in order]).label("last_rank"),
        func.lag(scoped.c.account_id).over(partition_by=partition, order_by=order).label("previous_account_id"),
        func.lag(scoped.c.input_tokens).over(partition_by=partition, order_by=order).label("previous_input_tokens"),
        func.lag(scoped.c.requested_epoch).over(partition_by=partition, order_by=order).label("previous_epoch"),
    ).subquery()
    dims = [ordered.c.bucket_epoch, ordered.c.keyed, _dimension_expr(ordered.c.thread_key)]
    sample = (ordered.c.status == "success") & (ordered.c.input_tokens > CACHE_MIN_INPUT_TOKENS)
    endpoints = [
        func.max(case((getattr(ordered.c, f"{prefix}_rank") == 1, column))).label(f"{prefix}_{name}")
        for prefix in ("first", "last")
        for name, column in (
            ("at", ordered.c.requested_at),
            ("id", ordered.c.id),
            ("account", ordered.c.account_id),
            ("input", ordered.c.input_tokens),
            ("epoch", ordered.c.requested_epoch),
        )
    ]
    headers = select(
        *(value.label(name) for value, name in zip(dims, KEYS)),
        func.count().label("requests"),
        func.coalesce(func.sum(case((sample, ordered.c.input_tokens), else_=0)), 0).label("cache_input_tokens"),
        func.coalesce(func.sum(case((sample, ordered.c.cached_input_tokens), else_=0)), 0).label(
            "cache_cached_input_tokens"
        ),
        *endpoints,
    ).group_by(*dims)
    account = _dimension_expr(ordered.c.account_id)
    accounts = select(
        *(value.label(name) for value, name in zip(dims, KEYS)),
        account.label("account_id"),
        func.count().label("requests"),
    ).group_by(*dims, account)
    pairs = (
        select(
            *(value.label(name) for value, name in zip(dims, KEYS)),
            ordered.c.previous_account_id.label("from_account"),
            ordered.c.account_id.label("to_account"),
            func.count().label("turns"),
        )
        .where(turn_condition(ordered))
        .group_by(*dims, ordered.c.previous_account_id, ordered.c.account_id)
    )
    return headers, accounts, pairs


async def _dirty(session: AsyncSession, epochs: list[int]) -> None:
    if not epochs:
        return
    table = ThreadIdentityDirtyHour
    stmt = _insert_fn(session)(table).values(
        [{"bucket_epoch": epoch, "generation": 1} for epoch in sorted(set(epochs))]
    )
    await session.execute(
        stmt.on_conflict_do_update(index_elements=["bucket_epoch"], set_={"generation": table.generation + 1})
    )


async def mark_thread_hours_dirty(session: AsyncSession, ids: list[int]) -> None:
    """Called before a bounded raw mutation, under the caller's fold lock."""
    watermark = (
        select(AccountUsageRollupState.thread_folded_through).where(AccountUsageRollupState.id == 1).scalar_subquery()
    )
    epochs = await session.scalars(
        select(_requested_at_epoch_bucket_expr(session, 3600))
        .where(
            RequestLog.id.in_(ids),
            RequestLog.requested_at < watermark,
        )
        .distinct()
    )
    await _dirty(session, list(epochs))


async def mark_late_thread_write(session: AsyncSession, requested_at: datetime) -> None:
    # Normal terminal persistence uses now. Historical imports must also mark
    # an hour currently being folded, not only an already committed watermark.
    if requested_at < floor_to_hour(utcnow() - FOLD_LAG):
        await _dirty(session, [epoch_seconds(floor_to_hour(requested_at))])


async def evict_pruned_thread_hours(session: AsyncSession) -> None:
    """Retention caches describe surviving raw rows, never permanent history."""
    first = await session.scalar(select(func.min(RequestLog.requested_at)))
    for table in (*TABLES, ThreadIdentityDirtyHour):
        stmt = delete(table)
        if first is not None:
            stmt = stmt.where(table.bucket_epoch < epoch_seconds(floor_to_hour(first)))
        await session.execute(stmt)


async def _rebuild(session: AsyncSession, start: datetime) -> None:
    epoch = epoch_seconds(start)
    marker = _insert_fn(session)(ThreadIdentityDirtyHour).values(bucket_epoch=epoch, generation=0)
    await session.execute(marker.on_conflict_do_nothing(index_elements=["bucket_epoch"]))
    # A historical append's marker upsert cannot commit during these scans.
    # If it waits here, its marker survives our commit and requests a new pass.
    await session.scalar(
        select(ThreadIdentityDirtyHour.generation)
        .where(ThreadIdentityDirtyHour.bucket_epoch == epoch)
        .with_for_update()
    )
    for table, source in zip(TABLES, hour_sources(_scoped(session, start, start + HOUR))):
        await session.execute(delete(table).where(table.bucket_epoch == epoch))
        await session.execute(insert(table).from_select(list(table.__table__.columns.keys()), source))
    await session.execute(delete(ThreadIdentityDirtyHour).where(ThreadIdentityDirtyHour.bucket_epoch == epoch))


async def fold_next_thread_slice(session: AsyncSession, target: datetime) -> bool:
    async with sqlite_writer_section():
        state = await _locked_state(session)
        if state is None:
            await session.execute(_state_bootstrap_stmt(session))
            await session.commit()
            state = await _locked_state(session)
        if state is None:
            return False
        dirty = await session.scalar(
            select(func.min(ThreadIdentityDirtyHour.bucket_epoch)).where(
                ThreadIdentityDirtyHour.bucket_epoch < epoch_seconds(target)
            )
        )
        if dirty is not None:
            await _rebuild(session, epoch_to_datetime(dirty))
            await session.commit()
            return True
        watermark = state.thread_folded_through
        if watermark >= target:
            return False
        next_at = await session.scalar(
            select(func.min(RequestLog.requested_at)).where(
                RequestLog.requested_at >= watermark, RequestLog.requested_at < target
            )
        )
        end = target
        if next_at is not None:
            start = max(watermark, floor_to_hour(next_at))
            end = min(start + HOUR, target)
            await _rebuild(session, start)
        await session.execute(
            update(AccountUsageRollupState).where(AccountUsageRollupState.id == 1).values(thread_folded_through=end)
        )
        await session.commit()
        return True


async def run_thread_fold_pass(*, now: datetime | None = None) -> int:
    target = floor_to_hour((now or utcnow()) - FOLD_LAG)
    committed = 0
    for _ in range(MAX_SLICES):
        async with get_background_session() as session:
            if not await fold_next_thread_slice(session, target):
                break
            committed += 1
    return committed


async def rekey_thread_accounts(session: AsyncSession, ids: list[str], target: str | None) -> None:
    """Soft detach/merge never remove rows, so adjacency is unchanged."""
    presence = ThreadIdentityAccountHour
    source = presence.account_id.in_([to_dimension(value) for value in ids])
    rows = (
        select(*(getattr(presence, key) for key in KEYS), literal(to_dimension(target)), func.sum(presence.requests))
        .where(source)
        .group_by(*(getattr(presence, key) for key in KEYS))
    )
    stmt = _insert_fn(session)(presence).from_select([*KEYS, "account_id", "requests"], rows)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[*KEYS, "account_id"], set_={"requests": presence.requests + stmt.excluded.requests}
        )
    )
    await session.execute(delete(presence).where(source))
    pairs = ThreadIdentityPairHour
    source_pairs = pairs.from_account.in_(ids) | pairs.to_account.in_(ids)
    if target is not None:
        mapped = [case((column.in_(ids), target), else_=column) for column in (pairs.from_account, pairs.to_account)]
        dims = [*(getattr(pairs, key) for key in KEYS), *mapped]
        rows = select(*dims, func.sum(pairs.turns)).where(source_pairs).group_by(*dims)
        stmt = _insert_fn(session)(pairs).from_select([*KEYS, "from_account", "to_account", "turns"], rows)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[*KEYS, "from_account", "to_account"], set_={"turns": pairs.turns + stmt.excluded.turns}
            )
        )
    await session.execute(delete(pairs).where(source_pairs))
    for prefix in ("first", "last"):
        column = getattr(ThreadIdentityHour, f"{prefix}_account")
        await session.execute(update(ThreadIdentityHour).where(column.in_(ids)).values({f"{prefix}_account": target}))
