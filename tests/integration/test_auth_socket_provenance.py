from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.core import shutdown as shutdown_state
from app.core.auth.dashboard_mode import get_dashboard_request_auth
from app.core.config.settings import get_settings
from app.core.middleware.dashboard_auth_proxy import add_dashboard_auth_proxy_middleware
from app.modules.health.api import router as health_router

pytestmark = pytest.mark.integration


def _configure_proxy(monkeypatch: pytest.MonkeyPatch, *, mode: str = "standard") -> None:
    monkeypatch.setenv("CODEX_LB_DASHBOARD_AUTH_MODE", mode)
    monkeypatch.setenv("CODEX_LB_FIREWALL_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("CODEX_LB_FIREWALL_TRUSTED_PROXY_CIDRS", "127.0.0.1/32,::1/128")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("peer", "forwarded", "expected"),
    [
        ("203.0.113.10", "127.0.0.1", 401),
        ("127.0.0.1", "203.0.113.10", 200),
        ("127.0.0.1", None, 200),
    ],
)
async def test_identity_proxy_trust_uses_raw_peer(app_instance, monkeypatch, peer, forwarded, expected):
    _configure_proxy(monkeypatch, mode="trusted_header")
    headers = {"Remote-User": "synthetic-proxy-user"}
    if forwarded is not None:
        headers["X-Forwarded-For"] = forwarded
    async with app_instance.router.lifespan_context(app_instance):
        transport = ASGITransport(app=app_instance, client=(peer, 12345))
        async with AsyncClient(transport=transport, base_url="http://lb.example") as client:
            response = await client.get("/api/settings", headers=headers)
    assert response.status_code == expected
    if expected == 401:
        assert response.json()["error"]["code"] == "proxy_auth_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [("GET", "status"), ("POST", "start"), ("POST", "stop")])
@pytest.mark.parametrize(
    ("peer", "forwarded", "host"),
    [
        ("203.0.113.10", "127.0.0.1", "localhost"),
        ("127.0.0.1", "203.0.113.10", "localhost"),
        ("127.0.0.1", "127.0.0.1", "localhost"),
        ("127.0.0.1", None, "lb.example"),
    ],
)
async def test_drain_rejects_projected_or_public_clients(
    app_instance, monkeypatch, method, path, peer, forwarded, host
):
    _configure_proxy(monkeypatch)
    headers = {} if forwarded is None else {"X-Forwarded-For": forwarded}
    transport = ASGITransport(app=app_instance, client=(peer, 12345))
    before = (shutdown_state.is_draining(), shutdown_state.is_shutdown_committed())
    async with AsyncClient(transport=transport, base_url=f"http://{host}") as client:
        response = await client.request(method, f"/internal/drain/{path}", headers=headers)
    assert response.status_code == 403
    assert (shutdown_state.is_draining(), shutdown_state.is_shutdown_committed()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [("Forwarded", "for=127.0.0.1")],
        [("X-Real-IP", "127.0.0.1")],
        [("True-Client-IP", "127.0.0.1")],
        [("CF-Connecting-IP", "127.0.0.1")],
        [("X-Forwarded-For", ""), ("X-Forwarded-For", "127.0.0.1")],
        [("Host", "")],
    ],
)
async def test_drain_requires_direct_local_host_and_no_proxy_hints(app_instance, monkeypatch, headers):
    _configure_proxy(monkeypatch)
    transport = ASGITransport(app=app_instance, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/internal/drain/status", headers=headers)
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("peer", ["127.0.0.1", "::1"])
@pytest.mark.parametrize("trust_proxy", ["true", "false"])
async def test_direct_loopback_operator_can_drain_and_resume(app_instance, monkeypatch, peer, trust_proxy):
    _configure_proxy(monkeypatch)
    monkeypatch.setenv("CODEX_LB_FIREWALL_TRUST_PROXY_HEADERS", trust_proxy)
    get_settings.cache_clear()
    transport = ASGITransport(app=app_instance, client=(peer, 12345))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        started = await client.post("/internal/drain/start")
        status = await client.get("/internal/drain/status")
        stopped = await client.post("/internal/drain/stop")
    assert started.status_code == 200, started.text
    assert status.status_code == 200, status.text
    assert stopped.status_code == 200, stopped.text
    assert status.json()["checks"]["draining"] == "true"
    assert not shutdown_state.is_draining()


@pytest.mark.asyncio
async def test_missing_socket_capture_cannot_authenticate_or_drain(monkeypatch):
    _configure_proxy(monkeypatch, mode="trusted_header")
    app = FastAPI()
    add_dashboard_auth_proxy_middleware(app)
    app.include_router(health_router)

    @app.get("/identity-check")
    async def identity_check(request: Request):
        return {
            "header_present": "Remote-User" in request.headers,
            "authenticated": get_dashboard_request_auth(request) is not None,
        }

    transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/identity-check", headers={"Remote-User": "synthetic-proxy-user"})
        assert response.json() == {"header_present": False, "authenticated": False}
        for method, path in (("GET", "status"), ("POST", "start"), ("POST", "stop")):
            denied = await client.request(method, f"/internal/drain/{path}")
            assert denied.status_code == 403
    request = Request({"type": "http", "client": ("127.0.0.1", 12345), "headers": [(b"remote-user", b"synthetic")]})
    assert request.headers["Remote-User"] == "synthetic"
    assert get_dashboard_request_auth(request) is None
