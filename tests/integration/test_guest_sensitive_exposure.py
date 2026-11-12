from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

import app.core.auth.dependencies as auth_dependencies
from app.core.auth.dashboard_access import (
    DashboardPrincipal,
    DashboardRole,
    Permission,
    Scope,
    guest_principal,
    legacy_permissions,
)
from app.core.auth.dashboard_mode import DashboardAuthMode
from app.core.utils.time import utcnow
from app.db.models import AutomationJob, AutomationRun, ModelSource
from app.db.session import SessionLocal
from app.modules.request_logs import repository as logs_repository
from app.modules.request_logs.repository import RequestLogsRepository
from app.modules.settings.repository import SettingsRepository

pytestmark = pytest.mark.integration

_VALIDATE_SESSION = auth_dependencies.validate_dashboard_session
_OPS_READ_PATHS = (
    "/api/automations",
    "/api/automations/options",
    "/api/automations/runs",
    "/api/automations/runs/options",
    "/api/automations/runs/private-run/details",
    "/api/automations/private-job/runs",
    "/api/model-sources/",
)


def _principal(*permissions: Permission) -> DashboardPrincipal:
    grants = {permission: Scope.ALL for permission in (Permission.DASHBOARD_READ, *permissions)}
    return DashboardPrincipal(
        role=DashboardRole.ADMIN,
        permissions=legacy_permissions(grants),
        auth_mode=DashboardAuthMode.STANDARD,
        grants=grants,
    )


def _use_principal(app: FastAPI, monkeypatch: pytest.MonkeyPatch, principal: DashboardPrincipal) -> None:
    monkeypatch.setattr(auth_dependencies, "validate_dashboard_session", AsyncMock(return_value=principal))
    monkeypatch.setitem(app.dependency_overrides, _VALIDATE_SESSION, lambda: principal)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _OPS_READ_PATHS)
@pytest.mark.parametrize("principal", [guest_principal(), _principal(Permission.CONVERSATIONS_READ)])
async def test_operational_inventory_requires_ops_grant(
    app_instance: FastAPI,
    async_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    principal: DashboardPrincipal,
) -> None:
    _use_principal(app_instance, monkeypatch, principal)
    response = await async_client.get(path)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "permission_required"
    assert response.json()["error"]["param"] == "ops:write"


@pytest.mark.asyncio
async def test_ops_grant_can_read_inventory_without_write_or_step_up(
    app_instance: FastAPI, async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, db_setup: None
) -> None:
    del db_setup
    now = utcnow()
    async with SessionLocal() as session:
        session.add(
            AutomationJob(
                id="private-job",
                name="private automation",
                schedule_time="12:00",
                schedule_timezone="UTC",
                model="gpt-5.1",
                prompt="private automation prompt",
            )
        )
        await session.flush()
        session.add(
            AutomationRun(
                id="private-run",
                job_id="private-job",
                trigger="manual",
                slot_key="private-slot",
                cycle_key="private-cycle",
                scheduled_for=now,
                started_at=now,
                finished_at=now,
                status="failed",
                error_message="private automation failure",
            )
        )
        session.add(ModelSource(id="private-source", name="private source", base_url="https://source.invalid/v1"))
        await session.commit()

    principal = _principal(Permission.OPS_WRITE)
    assert not principal.can(auth_dependencies.DashboardPermission.WRITE)
    _use_principal(app_instance, monkeypatch, principal)
    responses = {path: await async_client.get(path) for path in _OPS_READ_PATHS}
    for response in responses.values():
        assert response.status_code == 200, response.text
    assert responses["/api/automations"].json()["items"][0]["prompt"] == "private automation prompt"
    assert responses["/api/model-sources/"].json()["sources"][0]["baseUrl"] == "https://source.invalid/v1"
    assert responses["/api/automations/runs/private-run/details"].json()["run"]["id"] == "private-run"


@pytest.mark.asyncio
async def test_warmup_prompt_requires_ops_grant_without_changing_other_settings(
    app_instance: FastAPI, async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, db_setup: None
) -> None:
    del db_setup
    async with SessionLocal() as session:
        await SettingsRepository(session).update(limit_warmup_prompt="private warmup prompt")

    for principal, expected in (
        (_principal(Permission.OPS_WRITE), "private warmup prompt"),
        (guest_principal(), "[redacted]"),
        (_principal(Permission.CONVERSATIONS_READ), "[redacted]"),
        (_principal(Permission.OPS_WRITE), "private warmup prompt"),
    ):
        _use_principal(app_instance, monkeypatch, principal)
        response = await async_client.get("/api/settings")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["limitWarmupPrompt"] == expected
        assert isinstance(body["stickyThreadsEnabled"], bool)


@pytest.mark.asyncio
async def test_request_diagnostics_and_search_do_not_leak_across_permission_cache_keys(
    app_instance: FastAPI, async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, db_setup: None
) -> None:
    del db_setup
    monkeypatch.setattr(logs_repository, "_COUNT_CACHE_TTL_SECONDS", 30.0)
    logs_repository._clear_recent_count_cache()
    sentinel = "private-diagnostic-sentinel"
    async with SessionLocal() as session:
        await RequestLogsRepository(session).add_log(
            account_id=None,
            request_id="diagnostic-request",
            model="gpt-5.1",
            input_tokens=100,
            output_tokens=20,
            latency_ms=250,
            latency_first_token_ms=40,
            status="error",
            error_code="upstream_error",
            error_message=sentinel,
            failure_phase="upstream_connect",
            failure_detail="private failure detail",
            failure_exception_type="TimeoutError",
            upstream_status_code=502,
        )

    try:
        for principal, sensitive in (
            (_principal(Permission.CONVERSATIONS_READ), True),
            (guest_principal(), False),
            (_principal(Permission.OPS_WRITE), False),
            (_principal(Permission.CONVERSATIONS_READ), True),
        ):
            _use_principal(app_instance, monkeypatch, principal)
            response = await async_client.get("/api/request-logs", params={"limit": 10, "timeframe": "24h"})
            assert response.status_code == 200, response.text
            assert response.json()["total"] == 1
            row = response.json()["requests"][0]
            assert row["errorMessage"] == (sentinel if sensitive else None)
            assert row["failureDetail"] == ("private failure detail" if sensitive else None)
            assert row["errorCode"] == "upstream_error"
            assert row["failurePhase"] == "upstream_connect"
            assert row["failureExceptionType"] == "TimeoutError"
            assert row["upstreamStatusCode"] == 502
            assert row["status"] == "error"
            assert row["tokens"] == 120
            assert row["latencyMs"] == 250
            assert row["latencyFirstTokenMs"] == 40

            search = await async_client.get("/api/request-logs", params={"search": sentinel, "timeframe": "24h"})
            assert search.status_code == 200, search.text
            assert search.json()["total"] == int(sensitive)
            assert len(search.json()["requests"]) == int(sensitive)
    finally:
        logs_repository._clear_recent_count_cache()
