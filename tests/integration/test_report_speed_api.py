from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text

from app.db import dashboard_reads
from app.db.models import RequestLog
from app.db.session import SessionLocal, engine
from app.modules.reports import repository
from app.modules.reports.speed_histogram import run_speed_histogram_fold_pass

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


async def test_speed_endpoint_encloses_exact_medians_and_preserves_filters(async_client, db_setup):
    async with SessionLocal() as session:
        # Establish the fixture's complete retained-day coverage. Warmup
        # requests must not contribute to any speed metric.
        session.add(
            RequestLog(
                request_id="coverage-start",
                requested_at=datetime(2026, 11, 1),
                model="warmup",
                status="success",
                request_kind="warmup",
            )
        )
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
    for _ in range(4):
        await run_speed_histogram_fold_pass(now=datetime(2026, 11, 2, 3))
    response = await async_client.get("/api/reports/speed", params={**PARAMS, "model": "wanted"})
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["approximate"] is True
    assert body["unavailableReason"] is None
    assert body["coverageFrom"] == "2026-11-01T00:00:00Z"
    assert body["foldedThrough"].endswith("Z")
    assert len(body["daily"]) == 1
    row = body["daily"][0]
    for metric, estimate_key, exact in (
        ("ttft", "medianTtftMs", 30),
        ("tps", "medianTps", 100),
        ("queue", "medianQueueMs", 3),
    ):
        bounds = row[metric]
        assert bounds["sampleCount"] == 3
        assert bounds["lowerBound"] <= exact <= bounds["upperBound"]
        assert abs(row[estimate_key] - exact) <= exact * bounds["relativeErrorBound"]
    # A differently filtered request must not reuse the first filter's cache.
    excluded = await async_client.get("/api/reports/speed", params={**PARAMS, "model": "missing"})
    assert excluded.status_code == 200
    empty = excluded.json()["daily"][0]
    for metric in ("ttft", "tps", "queue"):
        assert empty[metric]["sampleCount"] == 0
        assert empty[metric]["lowerBound"] is None
        assert empty[metric]["upperBound"] is None


async def test_long_speed_range_does_not_scan_requests(async_client, db_setup, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Oversized speed window must not query raw requests")

    monkeypatch.setattr(repository.ReportsRepository, "aggregate_daily_speeds", forbidden)
    response = await async_client.get("/api/reports/speed", params={**PARAMS, "end_date": "2026-11-30"})
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["unavailableReason"] == "range_too_large"
    assert response.json()["daily"] == []


async def test_cold_speed_history_reports_building_without_scanning_raw(async_client, db_setup, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Cold histogram coverage must not fall back to raw medians")

    monkeypatch.setattr(repository, "_daily_speed_medians_stmt", forbidden)
    response = await async_client.get("/api/reports/speed", params=PARAMS)
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["unavailableReason"] == "building"
    assert response.json()["daily"] == []


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
