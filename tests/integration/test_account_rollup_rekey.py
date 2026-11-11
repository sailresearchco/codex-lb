"""Account lifecycle moves folded history even after its raw rows are pruned."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import Table, event, func, insert, select, update
from sqlalchemy.engine import Engine

from app.db.models import (
    Account,
    AccountUsageRollupState,
    RequestConversationHourlyRollup,
    RequestDemandQuarterRollup,
    RequestLog,
    RequestUsageHourlyErrorRollup,
    RequestUsageHourlyRollup,
)
from app.db.session import SessionLocal
from app.modules.accounts.deletion import run_account_deletion_pass
from app.modules.accounts.repository import AccountsRepository
from app.modules.accounts.usage_rollup import lock_fold_state
from app.modules.accounts.usage_time_rollup import (
    DIMENSION_SENTINEL,
    epoch_seconds,
    merge_time_rollups_into,
    to_dimension,
)
from tests.integration.test_request_usage_time_rollup import _make_account

pytestmark = pytest.mark.integration

_MODELS = (
    RequestUsageHourlyRollup,
    RequestUsageHourlyErrorRollup,
    RequestDemandQuarterRollup,
    RequestConversationHourlyRollup,
)
_START = datetime(2026, 11, 1)
_CANONICAL = "acc_rekey"
_DUPLICATES = ("acc_rekey__copy", DIMENSION_SENTINEL + "copy")
_UNRELATED = "acc_rekey_unrelated"
_IDENTITY = "rekey-shared-identity"
_EMAIL = "rekey@example.invalid"
type _Value = str | int | float | bool
type _Row = dict[str, _Value]
type _Snapshot = dict[str, list[_Row]]


async def _seed() -> _Snapshot:
    async with SessionLocal() as session:
        accounts = AccountsRepository(session)
        for index, account_id in enumerate((_CANONICAL, *_DUPLICATES, _UNRELATED)):
            account = _make_account(
                account_id,
                _EMAIL if account_id != _UNRELATED else "unrelated@example.invalid",
                chatgpt_account_id=_IDENTITY if account_id != _UNRELATED else "unrelated-identity",
            )
            account.created_at = _START + timedelta(seconds=index)
            await accounts.upsert(account, merge_by_email=False)
        await lock_fold_state(session)
        await session.execute(
            update(AccountUsageRollupState).values(hourly_folded_through=_START, conversation_folded_through=_START)
        )
        # These permanent buckets deliberately have no surviving raw rows.
        # All additive measures differ so a missing/derived measure is visible.
        measures = {
            "request_count": 11,
            "error_count": 2,
            "cancelled_count": 3,
            "input_tokens": 101,
            "output_tokens": 47,
            "reasoning_tokens": 13,
            "output_or_reasoning_tokens": 53,
            "cached_input_tokens": 19,
            "cached_input_tokens_clamped": 17,
            "cost_usd": 0.1,
            "cost_count": 7,
        }
        for model in _MODELS:
            table = cast(Table, model.__table__)
            keys = {column.name for column in table.primary_key}
            rows: list[_Row] = []
            for account_index, account_id in enumerate((_CANONICAL, *_DUPLICATES, _UNRELATED, None)):
                for bucket in range(2):
                    for variant, dimension in enumerate((None, "", DIMENSION_SENTINEL + "literal")):
                        for deleted in (False, True) if "is_deleted" in keys else (False,):
                            dimensions: _Row = {
                                "bucket_epoch": epoch_seconds(_START - timedelta(days=10)) + 3600 * bucket,
                                "slot_epoch": epoch_seconds(_START - timedelta(days=10)) + 900 * bucket,
                                "account_id": to_dimension(account_id),
                                "api_key_id": to_dimension(dimension),
                                "model": "model",
                                "service_tier": to_dimension(dimension),
                                "reasoning_effort": to_dimension(dimension),
                                "request_kind": ("normal", "warmup", "limit_warmup")[variant],
                                "status": ("success", "error", "cancelled")[variant],
                                "conversation_id": f"thread-{variant}",
                                "error_code": f"error-{variant}",
                                "is_deleted": deleted,
                            }
                            factor = 1 + account_index + bucket + variant + int(deleted)
                            rows.append(
                                {
                                    column.name: dimensions[column.name]
                                    if column.name in keys
                                    else measures[column.name] * factor
                                    for column in table.columns
                                }
                            )
            await session.execute(insert(model), rows)
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(RequestLog)) == 0
    return await _snapshot()


async def _snapshot() -> _Snapshot:
    async with SessionLocal() as session:
        return {
            model.__tablename__: [dict(row) for row in (await session.execute(select(model.__table__))).mappings()]
            for model in _MODELS
        }


def _expected(before: _Snapshot, sources: tuple[str, ...], target: str | None) -> _Snapshot:
    expected: _Snapshot = {}
    for model in _MODELS:
        table = cast(Table, model.__table__)
        keys = [column.name for column in table.primary_key]
        measures = [column.name for column in table.columns if column.name not in keys]
        grouped: dict[tuple[_Value, ...], _Row] = {}
        for original in before[model.__tablename__]:
            row = original.copy()
            if row["account_id"] in {to_dimension(source) for source in sources}:
                row["account_id"] = to_dimension(target)
                if target is None and "is_deleted" in row:
                    row["is_deleted"] = True
            key = tuple(row[name] for name in keys)
            if key in grouped:
                for name in measures:
                    grouped[key][name] = cast(int | float, grouped[key][name]) + cast(int | float, row[name])
            else:
                grouped[key] = row
        expected[model.__tablename__] = list(grouped.values())
    return expected


def _assert_snapshot(actual: _Snapshot, expected: _Snapshot) -> None:
    for model in _MODELS:
        keys = [column.name for column in cast(Table, model.__table__).primary_key]
        observed = {tuple(row[name] for name in keys): row for row in actual[model.__tablename__]}
        wanted = {tuple(row[name] for name in keys): row for row in expected[model.__tablename__]}
        assert observed.keys() == wanted.keys()
        for key, row in wanted.items():
            for name, value in row.items():
                assert observed[key][name] == (pytest.approx(value, abs=1e-9) if name == "cost_usd" else value)


@contextmanager
def _sql_only_rekey(*, interrupt: bool = False) -> Iterator[None]:
    tables = {model.__tablename__ for model in _MODELS}

    def before_execute(_connection, _cursor, statement, _parameters, _context, _executemany):
        sql = statement.lstrip().lower()
        if any(table in sql for table in tables):
            assert not sql.startswith("select"), "Lifecycle materialized folded account history into Python"
        if interrupt and sql.startswith("insert into request_demand_quarter_rollups"):
            raise RuntimeError("interrupt after two rollup tables")

    event.listen(Engine, "before_cursor_execute", before_execute)
    try:
        yield
    finally:
        event.remove(Engine, "before_cursor_execute", before_execute)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [False, True])
async def test_soft_delete_route_and_worker_rekey_pruned_history(async_client, db_setup, monkeypatch, interrupt):
    monkeypatch.setattr("app.modules.accounts.service.request_account_deletion_run", lambda: None)
    before = await _seed()
    response = await async_client.delete(f"/api/accounts/{_CANONICAL}")
    assert response.status_code == 200
    if interrupt:
        with _sql_only_rekey(interrupt=True):
            assert await run_account_deletion_pass() == {_CANONICAL: "error"}
        _assert_snapshot(await _snapshot(), before)
        async with SessionLocal() as session:
            pending = await session.get(Account, _CANONICAL)
            assert pending is not None and pending.delete_requested_at is not None
    with _sql_only_rekey():
        assert await run_account_deletion_pass() == {_CANONICAL: "finalized"}
        assert await run_account_deletion_pass() == {}
    _assert_snapshot(await _snapshot(), _expected(before, (_CANONICAL,), None))
    async with SessionLocal() as session:
        assert await session.get(Account, _CANONICAL) is None
        assert await session.scalar(select(func.count()).select_from(RequestLog)) == 0


async def _reauthenticate() -> None:
    async with SessionLocal() as session:
        saved = await AccountsRepository(session).upsert(
            _make_account(_CANONICAL, _EMAIL, chatgpt_account_id=_IDENTITY),
            merge_by_email=False,
            merge_by_chatgpt_identity=True,
        )
        assert saved.id == _CANONICAL


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [False, True])
async def test_identity_merge_rekeys_multiple_pruned_sources_atomically(db_setup, interrupt):
    before = await _seed()
    if interrupt:
        with _sql_only_rekey(interrupt=True), pytest.raises(RuntimeError, match="interrupt after two rollup tables"):
            await _reauthenticate()
        _assert_snapshot(await _snapshot(), before)
        async with SessionLocal() as session:
            for source in _DUPLICATES:
                assert await session.get(Account, source) is not None
    with _sql_only_rekey():
        await _reauthenticate()
        await _reauthenticate()
    _assert_snapshot(await _snapshot(), _expected(before, _DUPLICATES, _CANONICAL))
    async with SessionLocal() as session:
        for source in _DUPLICATES:
            assert await session.get(Account, source) is None
        assert await session.get(Account, _UNRELATED) is not None


@pytest.mark.asyncio
async def test_rekey_rejects_target_in_source_accounts_without_losing_history(db_setup):
    before = await _seed()
    with pytest.raises(ValueError, match="target must not be a source"):
        async with SessionLocal() as session:
            await lock_fold_state(session)
            await merge_time_rollups_into(session, _CANONICAL, [_CANONICAL])
    _assert_snapshot(await _snapshot(), before)
