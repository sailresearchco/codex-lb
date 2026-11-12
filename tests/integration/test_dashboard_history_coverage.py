from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, insert

from app.db.models import AccountUsageRollupState, RequestLog
from app.db.session import SessionLocal, engine
from app.modules.accounts.usage_time_rollup import run_conversation_fold_pass, run_hourly_fold_pass
from tests.integration.test_api_key_history_coverage import _count_work

pytestmark = pytest.mark.integration
NOW = datetime(2026, 11, 12, 12, 37)


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr("app.modules.dashboard.service.utcnow", lambda: NOW)


async def _seed(times):
    async with SessionLocal() as session:
        await session.execute(delete(AccountUsageRollupState))
        await session.execute(
            insert(RequestLog),
            [
                dict(
                    request_id=str(i),
                    requested_at=at,
                    model="model",
                    status="success",
                    conversation_id="thread",
                    input_tokens=10,
                    output_tokens=2,
                    cost_usd=0.25,
                    deleted_at=NOW if i % 2 else None,
                )
                for i, at in enumerate(times)
            ],
        )
        await session.commit()


async def _fold():
    while await run_hourly_fold_pass(now=NOW):
        pass
    while await run_conversation_fold_pass(now=NOW):
        pass


@pytest.mark.parametrize("ready_projection", [None, "hourly", "conversation"])
async def test_overview_requires_both_historical_projections_then_recovers(async_client, monkeypatch, ready_projection):
    count = 8000 if ready_projection is None else 2
    await _seed([NOW - timedelta(days=2)] * count)
    if ready_projection == "hourly":
        while await run_hourly_fold_pass(now=NOW):
            pass
    elif ready_projection == "conversation":
        while await run_conversation_fold_pass(now=NOW):
            pass
    counts, original = _count_work(monkeypatch)
    response = await async_client.get("/api/dashboard/overview", params={"timeframe": "7d"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "usage_history_building"
    if engine.dialect.name == "sqlite":
        assert 0 < counts[0] <= 25000
        from app.db import dashboard_reads

        monkeypatch.setattr(dashboard_reads._ReadBudget, "progress", original)
    await _fold()
    response = await async_client.get("/api/dashboard/overview", params={"timeframe": "7d"})
    assert response.status_code == 200, response.json()
    metrics = response.json()["summary"]["metrics"]
    assert metrics["requests"] == count  # Request metrics include soft deletion and null accounts.
    assert metrics["conversations"] == 1
    assert metrics["conversationRequests"] == (count + 1) // 2


async def test_overview_comparison_and_partial_hour_boundaries_remain_exact(async_client):
    await _seed(
        [
            NOW - timedelta(days=2) + timedelta(seconds=1),
            NOW - timedelta(days=1, seconds=1),
            NOW - timedelta(days=1),
            NOW - timedelta(hours=2),
        ]
    )
    response = await async_client.get("/api/dashboard/overview", params={"timeframe": "1d"})
    assert response.status_code == 503
    await _fold()
    response = await async_client.get("/api/dashboard/overview", params={"timeframe": "1d"})
    assert response.status_code == 200, response.json()
    summary = response.json()["summary"]
    assert summary["metrics"]["requests"] == 2
    assert summary["comparison"]["previous"]["requests"] == 2


async def test_overview_previous_period_alone_requires_coverage(async_client):
    await _seed([NOW - timedelta(days=10)])
    response = await async_client.get("/api/dashboard/overview", params={"timeframe": "7d"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "usage_history_building"
    await _fold()
    response = await async_client.get("/api/dashboard/overview", params={"timeframe": "7d"})
    assert response.status_code == 200
    assert response.json()["summary"]["metrics"]["requests"] == 0
    assert response.json()["summary"]["comparison"]["previous"]["requests"] == 1


@pytest.mark.parametrize("populated", [False, True])
async def test_overview_empty_or_recent_history_remains_available(async_client, populated):
    if populated:
        await _seed([NOW - timedelta(hours=3)])
    response = await async_client.get("/api/dashboard/overview", params={"timeframe": "7d"})
    assert response.status_code == 200, response.json()
    assert response.json()["summary"]["metrics"]["requests"] == int(populated)
