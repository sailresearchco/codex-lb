from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.core.auth.dashboard_access import guest_principal
from app.core.auth.dependencies import validate_dashboard_session
from app.db.models import ApiKey, RequestLog
from app.db.session import SessionLocal
from app.modules.accounts.usage_rollup import FOLD_LAG
from app.modules.reports.rollup import run_report_fold_pass
from app.modules.request_logs import api as logs_api
from app.modules.request_logs import repository as logs_repository

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 9, 12)


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(logs_api, "utcnow", lambda: NOW)
    monkeypatch.setattr("app.modules.dashboard.timeframes.utcnow", lambda: NOW)


@pytest.mark.parametrize(
    ("params", "start", "end"),
    [
        ({}, NOW - timedelta(hours=24), NOW),
        ({"timeframe": "1h"}, NOW - timedelta(hours=1), NOW),
        ({"timeframe": "7d"}, NOW - timedelta(days=7), NOW),
        ({"since": "2026-11-09T10:00:00Z"}, datetime(2026, 11, 9, 10), NOW),
        ({"until": "2020-01-02T00:00:00Z"}, datetime(2020, 1, 1), datetime(2020, 1, 2)),
        (
            {"since": "2020-01-01T02:00:00+02:00", "until": "2020-01-08T02:00:00+02:00"},
            datetime(2020, 1, 1),
            datetime(2020, 1, 8),
        ),
    ],
)
async def test_log_rows_counts_and_facets_share_finite_window(async_client, db_setup, fixed_clock, params, start, end):
    async with SessionLocal() as session:
        for label, timestamp in [
            ("before", start - timedelta(seconds=1)),
            ("start", start),
            ("end", end),
            ("after", end + timedelta(seconds=1)),
        ]:
            session.add(RequestLog(request_id=label, model=label, status="success", requested_at=timestamp))
        await session.commit()

    first = await async_client.get("/api/request-logs", params={**params, "limit": 1})
    second = await async_client.get("/api/request-logs", params={**params, "limit": 1, "offset": 1})
    options = await async_client.get("/api/request-logs/options", params=params)
    assert first.status_code == second.status_code == options.status_code == 200
    assert [row["requestId"] for row in first.json()["requests"]] == ["end"]
    assert [row["requestId"] for row in second.json()["requests"]] == ["start"]
    assert first.json()["total"] == second.json()["total"] == 2
    assert first.json()["hasMore"] is True
    assert second.json()["hasMore"] is False
    assert {row["model"] for row in options.json()["modelOptions"]} == {"start", "end"}


@pytest.mark.parametrize("path", ["/api/request-logs", "/api/request-logs/options"])
@pytest.mark.parametrize(
    "params",
    [
        {"since": "2020-01-01T00:00:00Z"},
        {"since": "2020-01-01T00:00:00Z", "until": "2020-01-08T00:00:00.000001Z"},
        {"since": "2020-01-02T00:00:00Z", "until": "2020-01-01T00:00:00Z"},
        {"timeframe": "7d", "until": "2026-11-10T12:00:00Z"},
        {"timeframe": "24h", "since": "2026-11-09T10:00:00Z"},
        {"since": "0001-01-01T00:00:00+01:00", "until": "0001-01-02T00:00:00Z"},
        {"until": "0001-01-01T00:00:00+01:00"},
        {"until": "0001-01-01T00:00:00Z"},
        {"since": "2020-02-30T00:00:00Z", "until": "2020-03-01T00:00:00Z"},
    ],
)
async def test_invalid_log_window_is_rejected_before_reads(async_client, fixed_clock, monkeypatch, path, params):
    monkeypatch.setattr(
        logs_repository.RequestLogsRepository, "list_recent", AsyncMock(side_effect=AssertionError("read"))
    )
    monkeypatch.setattr(
        logs_repository.RequestLogsRepository, "list_filter_options", AsyncMock(side_effect=AssertionError("read"))
    )
    assert (await async_client.get(path, params=params)).status_code == 422


async def test_rolling_count_cache_ignores_only_server_resolved_upper_bound(
    async_client, db_setup, fixed_clock, monkeypatch
):
    logs_repository._clear_recent_count_cache()
    monkeypatch.setattr(logs_repository, "_COUNT_CACHE_TTL_SECONDS", 30.0)
    count = AsyncMock(return_value=0)
    monkeypatch.setattr(logs_repository.RequestLogsRepository, "_count_recent", count)
    assert (await async_client.get("/api/request-logs", params={"search": "cache-window"})).status_code == 200
    later = NOW + timedelta(seconds=2)
    monkeypatch.setattr(logs_api, "utcnow", lambda: later)
    monkeypatch.setattr("app.modules.dashboard.timeframes.utcnow", lambda: later)
    assert (
        await async_client.get("/api/request-logs", params={"search": "cache-window", "timeframe": "24h"})
    ).status_code == 200
    assert count.await_count == 1
    for end in [NOW, NOW + timedelta(seconds=1)]:
        response = await async_client.get(
            "/api/request-logs", params={"search": "cache-window", "timeframe": "24h", "until": end.isoformat()}
        )
        assert response.status_code == 200
    assert count.await_count == 3


async def test_report_key_catalog_keeps_old_deleted_keys_and_never_leaks_cached_inventory(
    async_client, app_instance, db_setup, monkeypatch
):
    async with SessionLocal() as session:
        session.add(ApiKey(id="current", name="Current", key_hash="hash-current", key_prefix="sk-current"))
        session.add_all(
            [
                RequestLog(
                    request_id="old",
                    model="m",
                    api_key_id="deleted",
                    status="success",
                    requested_at=datetime(2020, 1, 2),
                ),
                RequestLog(
                    request_id="outside",
                    model="m",
                    api_key_id="outside",
                    status="success",
                    requested_at=datetime(2019, 1, 2),
                ),
            ]
        )
        await session.commit()
    await run_report_fold_pass(now=datetime(2020, 1, 8) + FOLD_LAG)
    params = {"start_date": "2020-01-01", "end_date": "2020-01-07", "timezone": "UTC", "api_key_id": "current"}
    admin = await async_client.get("/api/reports/options", params=params)
    assert admin.status_code == 200
    assert {key["id"] for key in admin.json()["apiKeys"]} == {"current", "deleted"}
    assert next(key for key in admin.json()["apiKeys"] if key["id"] == "deleted")["name"] == "deleted"
    from app.modules.reports.service import ReportsService

    monkeypatch.setattr(
        ReportsService, "get_api_key_options", AsyncMock(side_effect=AssertionError("guest inventory read"))
    )
    app_instance.dependency_overrides[validate_dashboard_session] = guest_principal
    try:
        guest = await async_client.get("/api/reports/options", params=params)
    finally:
        app_instance.dependency_overrides.pop(validate_dashboard_session, None)
    assert guest.status_code == 200
    assert guest.json()["apiKeys"] == []
