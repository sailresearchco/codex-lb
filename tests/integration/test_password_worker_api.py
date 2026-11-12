from __future__ import annotations

import asyncio
from threading import Event, get_ident

import pytest
from sqlalchemy import func, select

from app.core.auth import passwords
from app.db.models import RateLimitAttempt
from app.db.session import get_background_session

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_setup_hash_does_not_block_health_request(async_client, monkeypatch):
    started, release = Event(), Event()
    loop_thread = get_ident()
    real_hash = passwords.bcrypt.hashpw

    def blocking_hash(password: bytes, salt: bytes) -> bytes:
        assert get_ident() != loop_thread
        started.set()
        assert release.wait(5), "test did not release the password worker"
        return real_hash(password, salt)

    monkeypatch.setattr(passwords.bcrypt, "hashpw", blocking_hash)
    request = asyncio.create_task(
        async_client.post("/api/dashboard-auth/password/setup", json={"password": "synthetic-password"})
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        health = await asyncio.wait_for(async_client.get("/health/live"), 2)
        assert health.status_code == 200
        assert not request.done()
        busy = await asyncio.wait_for(
            async_client.post("/api/dashboard-auth/password/setup", json={"password": "other-password"}), 2
        )
        assert busy.status_code == 429
        assert busy.json()["error"]["code"] == "password_work_busy"
    finally:
        release.set()
        response = await asyncio.wait_for(request, 3)
    assert response.status_code == 200
    retry = await async_client.post("/api/dashboard-auth/password/setup", json={"password": "other-password"})
    assert retry.status_code == 409


@pytest.mark.asyncio
async def test_login_worker_keeps_health_responsive_and_bounds_concurrent_guesses(async_client, monkeypatch):
    setup = await async_client.post("/api/dashboard-auth/password/setup", json={"password": "synthetic-password"})
    assert setup.status_code == 200
    await async_client.post("/api/dashboard-auth/logout", json={})
    started, release = Event(), Event()
    checks = 0

    def blocking_check(password: bytes, hashed: bytes) -> bool:
        nonlocal checks
        checks += 1
        started.set()
        assert release.wait(5), "test did not release the password worker"
        return False

    monkeypatch.setattr(passwords.bcrypt, "checkpw", blocking_check)
    request = asyncio.create_task(async_client.post("/api/dashboard-auth/password/login", json={"password": "wrong"}))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        health = await asyncio.wait_for(async_client.get("/health/live"), 2)
        assert health.status_code == 200
        assert not request.done()
        busy = await asyncio.wait_for(
            async_client.post("/api/dashboard-auth/password/login", json={"password": "wrong-again"}), 2
        )
        assert busy.status_code == 429
        assert busy.json()["error"]["code"] == "password_work_busy"
        assert busy.headers["retry-after"] == "1"
        assert checks == 1
        # Existing persistent rate admission still precedes worker admission.
        async with get_background_session() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(RateLimitAttempt).where(RateLimitAttempt.type == "password")
                )
                == 2
            )
    finally:
        release.set()
        response = await asyncio.wait_for(request, 3)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    monkeypatch.setattr(passwords.bcrypt, "checkpw", lambda password, hashed: False)
    # Six remaining attempts reach bcrypt; the ninth hits the unchanged
    # persistent limit without dispatching more password work.
    for _ in range(6):
        response = await async_client.post("/api/dashboard-auth/password/login", json={"password": "wrong"})
        assert response.status_code == 401
    limited = await async_client.post("/api/dashboard-auth/password/login", json={"password": "wrong"})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "password_rate_limited"
