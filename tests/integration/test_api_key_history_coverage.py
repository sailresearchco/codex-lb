from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, insert, select

from app.db import dashboard_reads
from app.db.models import AccountUsageRollupState, ApiKey, RequestLog
from app.db.session import SessionLocal, engine
from app.modules.accounts.usage_rollup import run_fold_pass
from app.modules.api_keys.repository import ApiKeysRepository
from app.modules.usage.read_coverage import require_bounded_api_key_history

pytestmark = pytest.mark.integration
NOW = datetime(2026, 11, 12, 12)
EPOCH = datetime(1970, 1, 1)


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr("app.modules.api_keys.repository.utcnow", lambda: NOW)


async def _seed(*, at=NOW, count=1, kind="normal", key="key", watermark=None, deleted=False):
    async with SessionLocal() as session:
        session.add(ApiKey(id="key", name="Key", key_hash="hash", key_prefix="sk-test"))
        await session.execute(delete(AccountUsageRollupState))
        if watermark is not None:
            session.add(AccountUsageRollupState(id=1, folded_through=watermark))
        await session.execute(
            insert(RequestLog),
            [
                dict(
                    request_id=str(i),
                    requested_at=at,
                    model="model",
                    status="success",
                    request_kind=kind,
                    api_key_id=key,
                    input_tokens=10,
                    output_tokens=2,
                    cost_usd=0.25,
                    deleted_at=NOW if deleted else None,
                )
                for i in range(count)
            ],
        )
        await session.commit()


def _count_work(monkeypatch, cap=25000):
    counts = [0]
    original = dashboard_reads._ReadBudget.progress
    if engine.dialect.name == "sqlite":

        def progress(budget):
            counts[0] += 1
            return int(counts[0] > cap or original(budget))

        monkeypatch.setattr(dashboard_reads, "_PROGRESS_STEPS", 1)
        monkeypatch.setattr(dashboard_reads._ReadBudget, "progress", progress)
    return counts, original


@pytest.mark.parametrize("watermark", [None, EPOCH, NOW - timedelta(days=2)])
async def test_key_list_cold_history_defers_then_recovers_complete_totals(async_client, monkeypatch, watermark):
    # Key totals count detached AND soft-deleted rows, unlike account totals.
    count = 8000 if watermark is None else 1
    await _seed(at=NOW - timedelta(days=1), count=count, watermark=watermark, deleted=True)
    counts, original = _count_work(monkeypatch)
    response = await async_client.get("/api/api-keys")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "usage_history_building"
    if engine.dialect.name == "sqlite":
        assert 0 < counts[0] <= 25000
        monkeypatch.setattr(dashboard_reads._ReadBudget, "progress", original)
    assert await run_fold_pass(now=NOW) > 0
    response = await async_client.get("/api/api-keys")
    assert response.status_code == 200
    usage = response.json()[0]["usageSummary"]
    assert usage["requestCount"] == count
    assert usage["totalTokens"] == 12 * count
    assert usage["totalCostUsd"] == 0.25 * count
    async with SessionLocal() as session:
        assert len((await session.scalars(select(RequestLog.id))).all()) == count


@pytest.mark.parametrize("age", [timedelta(hours=3), timedelta(hours=4)])
async def test_key_list_recent_sparse_history_needs_no_fold(async_client, age):
    await _seed(at=NOW - age, watermark=EPOCH)
    response = await async_client.get("/api/api-keys")
    assert response.status_code == 200
    assert response.json()[0]["usageSummary"]["requestCount"] == 1


async def test_key_list_empty_inventory_ignores_deleted_key_history(async_client):
    await _seed(at=NOW - timedelta(days=30))
    async with SessionLocal() as session:
        await session.execute(delete(ApiKey))
        await session.commit()
    response = await async_client.get("/api/api-keys")
    assert response.status_code == 200
    assert response.json() == []


async def test_key_list_empty_database_remains_available(async_client):
    response = await async_client.get("/api/api-keys")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize("history", ["warmup", "orphan", "folded"])
async def test_key_list_seeks_past_twenty_thousand_irrelevant_rows(async_client, monkeypatch, history):
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite VM work regression")
    await _seed(
        at=NOW - timedelta(days=30),
        count=20000,
        kind="warmup" if history == "warmup" else "normal",
        key=None if history == "orphan" else "key",
    )
    if history == "folded":
        assert await run_fold_pass(now=NOW) > 0
    async with SessionLocal() as session:
        session.add(ApiKey(id="other", name="Other", key_hash="other-hash", key_prefix="other"))
        session.add_all(
            [
                RequestLog(
                    request_id=key,
                    requested_at=NOW,
                    model="model",
                    status="success",
                    request_kind="future-kind",
                    api_key_id=key,
                )
                for key in ("key", "other")
            ]
        )
        await session.commit()
    counts, _ = _count_work(monkeypatch)
    response = await async_client.get("/api/api-keys")
    assert response.status_code == 200, response.json()
    by_key = {row["id"]: row["usageSummary"]["requestCount"] for row in response.json()}
    assert by_key == {"key": 20001 if history == "folded" else 1, "other": 1}
    assert 0 < counts[0] <= 25000
    async with SessionLocal() as session:
        scoped = await ApiKeysRepository(session).list_usage_summary_by_key(["key"])
        assert set(scoped) == {"key"}
        assert scoped["key"].request_count == by_key["key"]


async def test_missing_watermark_bound_preserves_concurrent_pre_epoch_insert(async_client, monkeypatch):
    await _seed(kind="warmup", at=NOW - timedelta(days=30))
    inserted = False

    async def probe_then_insert(session, watermark, cutoff):
        nonlocal inserted
        await require_bounded_api_key_history(session, watermark, cutoff)
        if not inserted:
            # Insert between probe and aggregate, including the pre-1970
            # domain. The index-selection lower bound must not clamp history.
            session.add(
                RequestLog(
                    request_id="late",
                    requested_at=datetime(1960, 1, 1),
                    model="model",
                    status="success",
                    request_kind="future-kind",
                    api_key_id="key",
                )
            )
            await session.flush()
            inserted = True

    monkeypatch.setattr("app.modules.api_keys.repository.require_bounded_api_key_history", probe_then_insert)
    response = await async_client.get("/api/api-keys")
    assert response.status_code == 200
    assert response.json()[0]["usageSummary"]["requestCount"] == 1
