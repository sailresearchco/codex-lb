from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.core.auth.dependencies import validate_required_proxy_api_key_authorization
from app.core.utils.time import utcnow
from app.db import dashboard_reads
from app.db.models import ApiKeyLimit, LimitType, LimitWindow, RequestLog
from app.db.session import SessionLocal, engine
from app.modules.api_keys.repository import ApiKeysRepository
from app.modules.api_keys.service import ApiKeyCreateData, ApiKeysService

pytestmark = pytest.mark.integration
_EXPENSIVE_QUERY = (
    "WITH RECURSIVE rows(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM rows WHERE n < 100000000) SELECT sum(n) FROM rows"
)


async def _create_key(*, expired_limit: bool, historical_log: bool = False) -> tuple[str, str]:
    async with SessionLocal() as session:
        created = await ApiKeysService(ApiKeysRepository(session)).create_key(
            ApiKeyCreateData(name="self-usage-reader", allowed_models=None, usage_sections="", limits=[])
        )
    # Prime authentication before introducing the expired limit. The route's
    # self-service lookup must perform the reset even on an auth-cache hit.
    await validate_required_proxy_api_key_authorization(f"Bearer {created.key}")
    async with SessionLocal() as session:
        if expired_limit:
            session.add(
                ApiKeyLimit(
                    api_key_id=created.id,
                    limit_type=LimitType.CREDITS,
                    limit_window=LimitWindow.FIVE_HOURS,
                    max_value=100,
                    current_value=75,
                    reset_at=utcnow() - timedelta(hours=1),
                )
            )
        session.add(
            RequestLog(
                request_id="self-usage-request",
                api_key_id=created.id,
                requested_at=utcnow() - timedelta(days=30) if historical_log else utcnow(),
                model="model",
                status="success",
                input_tokens=11,
                output_tokens=2,
                cost_usd=0.25,
            )
        )
        await session.commit()
        return created.id, created.key


async def _assert_limit_reset_committed(key_id: str) -> None:
    async with SessionLocal() as session:
        limit = (await session.scalars(select(ApiKeyLimit).where(ApiKeyLimit.api_key_id == key_id))).one()
        assert limit.current_value == 0
        assert limit.reset_at > utcnow()


async def test_v1_self_usage_interrupts_only_read_then_recovers_with_complete_totals(async_client, monkeypatch):
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite read interrupt; PostgreSQL retains its driver deadline")
    assert engine.url.database not in (None, "", ":memory:")
    key_id, key = await _create_key(expired_limit=True)
    interrupted: list[int] = []

    async def slow_summary(self, *args, **kwargs):
        try:
            await self._session.execute(text(_EXPENSIVE_QUERY))
        except OperationalError as exc:
            assert isinstance(exc.orig, sqlite3.OperationalError)
            interrupted.append(exc.orig.sqlite_errorcode)
            raise
        raise AssertionError("Unbounded query completed")

    headers = {"Authorization": f"Bearer {key}"}
    with monkeypatch.context() as patch:
        patch.setattr(dashboard_reads, "_READ_BUDGET_SECONDS", 0.03)
        patch.setattr(ApiKeysRepository, "list_usage_summary_by_key", slow_summary)
        response = await async_client.get("/v1/usage", headers=headers)
    assert interrupted == [sqlite3.SQLITE_INTERRUPT]
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "dashboard_query_timeout"
    # The read failure must not undo the earlier expired-limit write or leave
    # an active interrupt handler on a returned connection.
    await _assert_limit_reset_committed(key_id)
    healthy = await async_client.get("/v1/usage", headers=headers)
    assert healthy.status_code == 200
    payload = healthy.json()
    assert payload["request_count"] == 1
    assert payload["total_tokens"] == 13
    assert payload["total_cost_usd"] == 0.25
    assert payload["limits"][0]["current_value"] == 0
    assert payload["limits"][0]["remaining_value"] == 100


@pytest.mark.parametrize("path", ["/api/codex/usage", "/api/codex/usage/"])
async def test_codex_self_usage_reads_limits_without_history_and_commits_reset(async_client, monkeypatch, path):
    key_id, key = await _create_key(expired_limit=True, historical_log=True)

    async def forbidden_history(self, *args, **kwargs):
        raise AssertionError("Codex quota must not aggregate lifetime request history")

    monkeypatch.setattr(ApiKeysRepository, "get_usage_summary_by_key_id", forbidden_history)
    # No read budget may accidentally wrap auth or the expired-limit reset.
    monkeypatch.setattr(dashboard_reads, "_READ_BUDGET_SECONDS", -1)
    response = await async_client.get(path, headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["plan_type"] == "api_key"
    assert payload["rate_limit"]["primary_window"]["used_percent"] == 0
    assert "request_count" not in payload
    await _assert_limit_reset_committed(key_id)


async def test_self_usage_missing_key_and_empty_limits_remain_distinct(async_client):
    key_id, key = await _create_key(expired_limit=False)
    async with SessionLocal() as session:
        service = ApiKeysService(ApiKeysRepository(session))
        assert await service.get_key_limits_for_self("missing") is None
        assert await service.get_key_limits_for_self(key_id) == []
    response = await async_client.get("/api/codex/usage", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200
    assert response.json()["plan_type"] == "api_key"
    for path in ("/v1/usage", "/api/codex/usage", "/api/codex/usage/"):
        response = await async_client.get(path, headers={"Authorization": "Bearer sk-clb-invalid"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_api_key"
