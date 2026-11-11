"""Transactional conversation summaries of retained request logs.

Appends use atomic per-conversation upserts. Other mutations and backfill
hold the existing fold-state lock, then lock headers in conversation-ID
order on PostgreSQL. Raw changes, projection deltas, and inclusion markers
commit together. Retention subtracts contributions: these are not permanent
usage rollups. No request path builds or repairs the projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import case, delete, func, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.exceptions import DashboardServiceUnavailableError
from app.core.utils.time import utcnow
from app.db.models import (
    AccountUsageRollupState,
    RequestConversationFacet,
    RequestConversationModelSummary,
    RequestConversationSummary,
    RequestLog,
)
from app.db.session import get_background_session, sqlite_writer_section
from app.modules.accounts.usage_rollup import _insert_fn, lock_fold_state
from app.modules.accounts.usage_time_rollup import _dimension_expr, from_dimension, to_dimension

BACKFILL_BATCH_SIZE = 500
BACKFILL_BATCHES_PER_PASS = 20
_KINDS = ("warmup", "limit_warmup")
_FACETS = {
    "account": RequestLog.account_id,
    "api_key": RequestLog.api_key_id,
    "model": RequestLog.model,
    "useragent": RequestLog.useragent_group,
}
_SUMS = ("request_count", "total_tokens", "cached_input_tokens", "cached_known_count", "cost_usd")
_MODEL_SUMS = (
    "request_count",
    "total_elapsed_ms",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cached_known_count",
    "cost_usd",
)


@dataclass(frozen=True, slots=True)
class SummaryContribution:
    conversation_id: str
    first_requested_at: datetime
    last_requested_at: datetime
    request_count: int
    total_tokens: int
    cached_input_tokens: int
    cached_known_count: int
    cost_usd: float


@dataclass(frozen=True, slots=True)
class FacetContribution:
    conversation_id: str
    kind: str
    value: str
    request_count: int
    last_requested_at: datetime


@dataclass(frozen=True, slots=True)
class ModelContribution:
    conversation_id: str
    model: str
    reasoning_effort: str
    request_count: int
    total_elapsed_ms: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cached_known_count: int
    cost_usd: float
    last_requested_at: datetime


@dataclass(frozen=True, slots=True)
class Contributions:
    summaries: tuple[SummaryContribution, ...] = ()
    facets: tuple[FacetContribution, ...] = ()
    models: tuple[ModelContribution, ...] = ()


def eligible_conditions() -> tuple[ColumnElement[bool], ...]:
    return (
        RequestLog.conversation_summary_included.is_(True),
        RequestLog.deleted_at.is_(None),
        # Constants match the partial boundary index at prepare time. SQLite
        # cannot prove a parameterized NOT IN predicate implies its WHERE.
        RequestLog.request_kind.not_in((literal_column("'warmup'"), literal_column("'limit_warmup'"))),
        RequestLog.conversation_id.is_not(None),
        RequestLog.conversation_id != literal_column("''"),
    )


def _cached_expr(session: AsyncSession):
    least = func.least if session.get_bind().dialect.name == "postgresql" else func.min
    greatest = func.greatest if session.get_bind().dialect.name == "postgresql" else func.max
    return case(
        (RequestLog.cached_input_tokens.is_(None), None),
        (RequestLog.input_tokens.is_(None), greatest(0, RequestLog.cached_input_tokens)),
        else_=greatest(0, least(RequestLog.cached_input_tokens, RequestLog.input_tokens)),
    )


async def require_ready(session: AsyncSession) -> None:
    resetting = await session.scalar(
        select(AccountUsageRollupState.conversation_reset_after_id).where(AccountUsageRollupState.id == 1)
    )
    pending = await session.scalar(
        select(RequestLog.id).where(RequestLog.conversation_summary_included.is_(False)).limit(1)
    )
    if resetting is not None or pending is not None:
        raise DashboardServiceUnavailableError(
            "Conversation summaries are being built. Retry shortly.", code="conversation_summaries_building"
        )


async def _lock_headers(session: AsyncSession, ids: list[str]) -> None:
    if ids and session.get_bind().dialect.name == "postgresql":
        await session.execute(
            select(RequestConversationSummary.conversation_id)
            .where(RequestConversationSummary.conversation_id.in_(ids))
            .order_by(RequestConversationSummary.conversation_id)
            .with_for_update()
        )


async def capture_contributions(session: AsyncSession, *conditions: ColumnElement[bool]) -> Contributions:
    """Capture included rows before a mutation; caller holds fold-state lock.

    Bounded deletion batches must materialize their ID set before calling:
    executing a moving LIMIT subquery again after deletion would target the
    next batch. Bulk account deletion also fences FK inserts with its account
    row lock. Appends may proceed on unrelated conversations.
    """
    where = (*eligible_conditions(), *conditions)
    # eligible_conditions excludes NULL; the ORM's column type remains nullable.
    ids = cast(
        list[str], list((await session.scalars(select(RequestLog.conversation_id).where(*where).distinct())).all())
    )
    if not ids:
        return Contributions()
    await _lock_headers(session, ids)
    cached = _cached_expr(session)
    summaries = tuple(
        SummaryContribution(*row)
        for row in (
            await session.execute(
                select(
                    RequestLog.conversation_id,
                    func.min(RequestLog.requested_at),
                    func.max(RequestLog.requested_at),
                    func.count(),
                    func.sum(
                        func.coalesce(RequestLog.input_tokens, 0)
                        + func.coalesce(RequestLog.output_tokens, RequestLog.reasoning_tokens, 0)
                    ),
                    func.coalesce(func.sum(cached), 0),
                    func.count(cached),
                    func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                )
                .where(*where)
                .group_by(RequestLog.conversation_id)
            )
        ).all()
    )
    facets = []
    for kind, column in _FACETS.items():
        rows = await session.execute(
            select(RequestLog.conversation_id, column, func.count(), func.max(RequestLog.requested_at))
            .where(*where, column.is_not(None))
            .group_by(RequestLog.conversation_id, column)
        )
        facets.extend(FacetContribution(cid, kind, value, count, last) for cid, value, count, last in rows)
    effort = _dimension_expr(RequestLog.reasoning_effort)
    models = tuple(
        ModelContribution(*row)
        for row in (
            await session.execute(
                select(
                    RequestLog.conversation_id,
                    RequestLog.model,
                    effort,
                    func.count(),
                    func.coalesce(func.sum(RequestLog.latency_ms), 0),
                    func.coalesce(func.sum(RequestLog.input_tokens), 0),
                    func.sum(func.coalesce(RequestLog.output_tokens, RequestLog.reasoning_tokens, 0)),
                    func.coalesce(func.sum(cached), 0),
                    func.count(cached),
                    func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                    func.max(RequestLog.requested_at),
                )
                .where(*where)
                .group_by(RequestLog.conversation_id, RequestLog.model, effort)
            )
        ).all()
    )
    return Contributions(summaries, tuple(facets), models)


def contributions_from_logs(logs: list[RequestLog]) -> Contributions:
    summaries: dict[str, SummaryContribution] = {}
    facets: dict[tuple[str, str, str], FacetContribution] = {}
    models: dict[tuple[str, str, str], ModelContribution] = {}
    for log in logs:
        cid = log.conversation_id
        if not cid or log.deleted_at is not None or log.request_kind in _KINDS:
            continue
        cached = log.cached_input_tokens
        if cached is not None:
            cached = max(0, cached if log.input_tokens is None else min(cached, log.input_tokens))
        tokens = (log.input_tokens or 0) + (
            log.output_tokens if log.output_tokens is not None else log.reasoning_tokens or 0
        )
        old = summaries.get(cid)
        summaries[cid] = SummaryContribution(
            cid,
            min(old.first_requested_at, log.requested_at) if old else log.requested_at,
            max(old.last_requested_at, log.requested_at) if old else log.requested_at,
            (old.request_count if old else 0) + 1,
            (old.total_tokens if old else 0) + tokens,
            (old.cached_input_tokens if old else 0) + (cached or 0),
            (old.cached_known_count if old else 0) + int(cached is not None),
            (old.cost_usd if old else 0.0) + (log.cost_usd or 0.0),
        )
        model_key = (cid, log.model, to_dimension(log.reasoning_effort))
        prior_model = models.get(model_key)
        models[model_key] = ModelContribution(
            *model_key,
            (prior_model.request_count if prior_model else 0) + 1,
            (prior_model.total_elapsed_ms if prior_model else 0) + (log.latency_ms or 0),
            (prior_model.input_tokens if prior_model else 0) + (log.input_tokens or 0),
            (prior_model.output_tokens if prior_model else 0)
            + (log.output_tokens if log.output_tokens is not None else log.reasoning_tokens or 0),
            (prior_model.cached_input_tokens if prior_model else 0) + (cached or 0),
            (prior_model.cached_known_count if prior_model else 0) + int(cached is not None),
            (prior_model.cost_usd if prior_model else 0.0) + (log.cost_usd or 0.0),
            max(prior_model.last_requested_at, log.requested_at) if prior_model else log.requested_at,
        )
        for kind, column in _FACETS.items():
            value = getattr(log, column.key)
            if value is None:
                continue
            key = (cid, kind, value)
            prior = facets.get(key)
            facets[key] = FacetContribution(
                cid,
                kind,
                value,
                (prior.request_count if prior else 0) + 1,
                max(prior.last_requested_at, log.requested_at) if prior else log.requested_at,
            )
    return Contributions(tuple(summaries.values()), tuple(facets.values()), tuple(models.values()))


async def add_contributions(session: AsyncSession, contributions: Contributions) -> None:
    insert = _insert_fn(session)
    least = func.least if session.get_bind().dialect.name == "postgresql" else func.min
    greatest = func.greatest if session.get_bind().dialect.name == "postgresql" else func.max
    header = RequestConversationSummary
    rows = sorted(contributions.summaries, key=lambda item: item.conversation_id)
    for start in range(0, len(rows), 500):
        values = [
            {
                name: getattr(row, name)
                for name in (*_SUMS, "conversation_id", "first_requested_at", "last_requested_at")
            }
            for row in rows[start : start + 500]
        ]
        stmt = insert(header).values(values)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[header.conversation_id],
                set_={
                    **{name: getattr(header, name) + getattr(stmt.excluded, name) for name in _SUMS},
                    "first_requested_at": least(header.first_requested_at, stmt.excluded.first_requested_at),
                    "last_requested_at": greatest(header.last_requested_at, stmt.excluded.last_requested_at),
                },
            )
        )
    facet = RequestConversationFacet
    facet_rows = sorted(contributions.facets, key=lambda item: (item.conversation_id, item.kind, item.value))
    for start in range(0, len(facet_rows), 500):
        stmt = insert(facet).values(
            [
                {
                    "conversation_id": row.conversation_id,
                    "kind": row.kind,
                    "value": row.value,
                    "request_count": row.request_count,
                    "last_requested_at": row.last_requested_at,
                }
                for row in facet_rows[start : start + 500]
            ]
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[facet.conversation_id, facet.kind, facet.value],
                set_={
                    "request_count": facet.request_count + stmt.excluded.request_count,
                    "last_requested_at": greatest(facet.last_requested_at, stmt.excluded.last_requested_at),
                },
            )
        )
    model = RequestConversationModelSummary
    model_rows = sorted(
        contributions.models, key=lambda item: (item.conversation_id, item.model, item.reasoning_effort)
    )
    keys = ("conversation_id", "model", "reasoning_effort")
    for start in range(0, len(model_rows), 500):
        stmt = insert(model).values(
            [
                {name: getattr(row, name) for name in (*keys, *_MODEL_SUMS, "last_requested_at")}
                for row in model_rows[start : start + 500]
            ]
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[getattr(model, name) for name in keys],
                set_={
                    **{name: getattr(model, name) + getattr(stmt.excluded, name) for name in _MODEL_SUMS},
                    "last_requested_at": greatest(model.last_requested_at, stmt.excluded.last_requested_at),
                },
            )
        )


async def subtract_contributions(session: AsyncSession, contributions: Contributions) -> None:
    """Subtract after raw mutation, repairing only extrema that were removed."""
    header = RequestConversationSummary
    for row in contributions.summaries:
        current = (
            await session.execute(
                select(header.first_requested_at, header.last_requested_at, header.request_count).where(
                    header.conversation_id == row.conversation_id
                )
            )
        ).one()
        if current.request_count == row.request_count:
            await session.execute(delete(header).where(header.conversation_id == row.conversation_id))
            continue
        values = {name: getattr(header, name) - getattr(row, name) for name in _SUMS}
        base = select(RequestLog.requested_at).where(
            *eligible_conditions(),
            RequestLog.conversation_id == row.conversation_id,
        )
        if row.first_requested_at == current.first_requested_at:
            values["first_requested_at"] = base.order_by(RequestLog.requested_at).limit(1).scalar_subquery()
        if row.last_requested_at == current.last_requested_at:
            values["last_requested_at"] = base.order_by(RequestLog.requested_at.desc()).limit(1).scalar_subquery()
        await session.execute(update(header).where(header.conversation_id == row.conversation_id).values(**values))
    facet = RequestConversationFacet
    for row in contributions.facets:
        where = (facet.conversation_id == row.conversation_id, facet.kind == row.kind, facet.value == row.value)
        current = (await session.execute(select(facet.request_count, facet.last_requested_at).where(*where))).one()
        if current.request_count == row.request_count:
            await session.execute(delete(facet).where(*where))
            continue
        values = {"request_count": facet.request_count - row.request_count}
        if row.last_requested_at == current.last_requested_at:
            values["last_requested_at"] = (
                select(RequestLog.requested_at)
                .where(
                    *eligible_conditions(),
                    RequestLog.conversation_id == row.conversation_id,
                    _FACETS[row.kind] == row.value,
                )
                .order_by(RequestLog.requested_at.desc())
                .limit(1)
                .scalar_subquery()
            )
        await session.execute(update(facet).where(*where).values(**values))
    model = RequestConversationModelSummary
    for row in contributions.models:
        where = (
            model.conversation_id == row.conversation_id,
            model.model == row.model,
            model.reasoning_effort == row.reasoning_effort,
        )
        current = (await session.execute(select(model.request_count, model.last_requested_at).where(*where))).one()
        if current.request_count == row.request_count:
            await session.execute(delete(model).where(*where))
            continue
        values = {name: getattr(model, name) - getattr(row, name) for name in _MODEL_SUMS}
        if row.last_requested_at == current.last_requested_at:
            effort = from_dimension(row.reasoning_effort)
            values["last_requested_at"] = (
                select(RequestLog.requested_at)
                .where(
                    *eligible_conditions(),
                    RequestLog.conversation_id == row.conversation_id,
                    RequestLog.model == row.model,
                    RequestLog.reasoning_effort.is_(None) if effort is None else RequestLog.reasoning_effort == effort,
                )
                .order_by(RequestLog.requested_at.desc())
                .limit(1)
                .scalar_subquery()
            )
        await session.execute(update(model).where(*where).values(**values))


async def rekey_account_facets(session: AsyncSession, source_ids: list[str], target_id: str) -> None:
    """Account merge only changes one facet; header measures stay identical."""
    facet = RequestConversationFacet
    after: str | None = None
    while True:
        where = [facet.kind == "account", facet.value.in_(source_ids)]
        if after is not None:
            where.append(facet.conversation_id > after)
        ids = list(
            (
                await session.scalars(
                    select(facet.conversation_id).where(*where).distinct().order_by(facet.conversation_id).limit(500)
                )
            ).all()
        )
        if not ids:
            return
        await _lock_headers(session, ids)
        where.append(facet.conversation_id.in_(ids))
        rows = (
            await session.execute(
                select(facet.conversation_id, func.sum(facet.request_count), func.max(facet.last_requested_at))
                .where(*where)
                .group_by(facet.conversation_id)
            )
        ).all()
        await session.execute(delete(facet).where(*where))
        await add_contributions(
            session,
            Contributions(
                facets=tuple(FacetContribution(cid, "account", target_id, count, last) for cid, count, last in rows)
            ),
        )
        after = ids[-1]


async def remove_account_history(session: AsyncSession, account_id: str, *, delete_history: bool) -> None:
    """Bound Python state while retaining the caller's single atomic deletion."""
    from app.modules.reports.thread_rollup import mark_thread_hours_dirty

    deleted_at = utcnow()
    while True:
        ids = list(
            (
                await session.scalars(
                    select(RequestLog.id)
                    .where(RequestLog.account_id >= account_id, RequestLog.account_id <= account_id)
                    .order_by(
                        RequestLog.account_id,
                        RequestLog.request_kind,
                        RequestLog.deleted_at,
                        RequestLog.requested_at,
                        RequestLog.id,
                    )
                    .limit(500)
                )
            ).all()
        )
        if not ids:
            return
        await mark_thread_hours_dirty(session, ids)
        before = await capture_contributions(session, RequestLog.id.in_(ids))
        if delete_history:
            await session.execute(delete(RequestLog).where(RequestLog.id.in_(ids)))
        else:
            await session.execute(
                update(RequestLog).where(RequestLog.id.in_(ids)).values(account_id=None, deleted_at=deleted_at)
            )
        await subtract_contributions(session, before)


async def backfill_conversation_summaries(*, limit: int = BACKFILL_BATCH_SIZE) -> int:
    """One resumable transaction; raw inclusion flags are the durable cursor."""
    if not 1 <= limit <= 1000:
        raise ValueError("summary backfill limit must be between 1 and 1000")
    async with get_background_session() as session:
        async with sqlite_writer_section():
            await lock_fold_state(session)
            if (
                await session.scalar(
                    select(AccountUsageRollupState.conversation_reset_after_id).where(AccountUsageRollupState.id == 1)
                )
                is not None
            ):
                return 0
            rows = list(
                (
                    await session.scalars(
                        select(RequestLog)
                        .where(RequestLog.conversation_summary_included.is_(False))
                        .order_by(RequestLog.id)
                        .limit(limit)
                        .with_for_update()
                    )
                ).all()
            )
            await add_contributions(session, contributions_from_logs(rows))
            if rows:
                await session.execute(
                    update(RequestLog)
                    .where(RequestLog.id.in_([row.id for row in rows]))
                    .values(conversation_summary_included=True)
                )
            await session.commit()
            return len(rows)


async def run_conversation_summary_pass() -> int:
    total = 0
    for _ in range(BACKFILL_BATCHES_PER_PASS):
        count = await backfill_conversation_summaries()
        total += count
        if count < BACKFILL_BATCH_SIZE:
            break
    return total
