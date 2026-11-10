from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text

from app.db import dashboard_reads
from app.db.models import RequestLog
from app.db.session import SessionLocal, engine
from app.modules.reports import repository

pytestmark = pytest.mark.integration

PARAMS = {"start_date": "2026-11-01", "end_date": "2026-11-01", "timezone": "UTC"}


async def test_cost_report_does_not_run_speed_calculation(async_client, db_setup, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Cost totals must not calculate speed medians")

    monkeypatch.setattr(repository, "_daily_speed_medians_stmt", forbidden)
    response = await async_client.get("/api/reports", params=PARAMS)
    assert response.status_code == 200
    assert response.json()["summary"]["totalRequests"] == 0
    assert response.json()["speedMetricsAvailable"] is False


async def test_speed_endpoint_preserves_exact_medians_and_filters(async_client, db_setup):
    async with SessionLocal() as session:
        for i, ttft in enumerate((10, 30, 50)):
            session.add(
                RequestLog(
                    request_id=f"speed-{i}",
                    requested_at=datetime(2026, 11, 1, 12),
                    model="wanted",
                    status="success",
                    output_tokens=100,
                    reasoning_tokens=0,
                    latency_ms=ttft + 1000,
                    latency_first_token_ms=ttft,
                    latency_queue_ms=2 * i + 1,
                )
            )
        session.add(
            RequestLog(
                request_id="other-model",
                requested_at=datetime(2026, 11, 1, 12),
                model="excluded",
                status="success",
                output_tokens=100,
                latency_ms=99999,
                latency_first_token_ms=9999,
                latency_queue_ms=999,
            )
        )
        await session.commit()
    response = await async_client.get("/api/reports/speed", params={**PARAMS, "model": "wanted"})
    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "maxDays": 7,
        "daily": [{"date": "2026-11-01", "medianTtftMs": 30.0, "medianTps": 100.0, "medianQueueMs": 3.0}],
    }
    # A differently filtered request must not reuse the first filter's cache.
    excluded = await async_client.get("/api/reports/speed", params={**PARAMS, "model": "missing"})
    assert excluded.status_code == 200
    assert excluded.json()["daily"] == [
        {"date": "2026-11-01", "medianTtftMs": 0.0, "medianTps": 0.0, "medianQueueMs": 0.0}
    ]


async def test_long_speed_range_does_not_scan_requests(async_client, db_setup, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Oversized speed window must not query raw requests")

    monkeypatch.setattr(repository.ReportsRepository, "aggregate_daily_speeds", forbidden)
    response = await async_client.get("/api/reports/speed", params={**PARAMS, "end_date": "2026-11-30"})
    assert response.status_code == 200
    assert response.json() == {"available": False, "maxDays": 7, "daily": []}


async def test_speed_timeout_does_not_block_cost_report(async_client, db_setup, monkeypatch):
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite execution-budget regression")
    monkeypatch.setattr(dashboard_reads, "_READ_BUDGET_SECONDS", 0.02)

    async def slow(self, *args, **kwargs):
        await self._session.execute(
            text(
                "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n"
            )
        )
        raise AssertionError("Read budget did not interrupt the query")

    monkeypatch.setattr(repository.ReportsRepository, "aggregate_daily_speeds", slow)
    response = await async_client.get("/api/reports/speed", params=PARAMS)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "dashboard_query_timeout"
    monkeypatch.setattr(dashboard_reads, "_READ_BUDGET_SECONDS", 15)
    report = await async_client.get("/api/reports", params=PARAMS)
    assert report.status_code == 200


@pytest.mark.parametrize(
    "params",
    [
        {**PARAMS, "start_date": "2026-11-02"},
        {**PARAMS, "start_date": "invalid"},
    ],
)
async def test_speed_rejects_invalid_ranges(async_client, db_setup, params):
    response = await async_client.get("/api/reports/speed", params=params)
    assert response.status_code in (400, 422)
