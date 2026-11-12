from __future__ import annotations

import asyncio
from concurrent.futures import Future
from threading import Event, get_ident

import pytest

from app.core.auth import passwords
from app.core.exceptions import DashboardRateLimitError


async def _wait_for_worker_release() -> None:
    async with asyncio.timeout(2):
        while passwords._capacity.locked():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_cancelled_password_request_keeps_capacity_until_thread_finishes(monkeypatch):
    started, release = Event(), Event()
    checks = 0
    loop_thread = get_ident()

    def blocking_check(password: bytes, hashed: bytes) -> bool:
        nonlocal checks
        checks += 1
        assert get_ident() != loop_thread
        started.set()
        assert release.wait(5), "test did not release the password worker"
        return True

    monkeypatch.setattr(passwords.bcrypt, "checkpw", blocking_check)
    request = asyncio.create_task(passwords.check_password("secret", "stored-hash"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        request.cancel()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        for _ in range(3):
            with pytest.raises(DashboardRateLimitError, match="busy") as error:
                await passwords.check_password("other", "stored-hash")
            assert error.value.code == "password_work_busy"
        assert checks == 1
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        await _wait_for_worker_release()
    assert await passwords.check_password("secret", "stored-hash") is True
    assert checks == 2


def test_worker_capacity_survives_request_loop_shutdown(monkeypatch):
    submitted: list[Future[bool]] = []

    class HeldExecutor:
        def submit(self, work):
            future: Future[bool] = Future()
            assert future.set_running_or_notify_cancel()
            submitted.append(future)
            return future

    monkeypatch.setattr(passwords, "_executor", HeldExecutor())

    async def abandoned_request() -> None:
        task = asyncio.create_task(passwords.check_password("secret", "stored-hash"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(abandoned_request())
        with pytest.raises(DashboardRateLimitError):
            asyncio.run(passwords.check_password("other", "stored-hash"))
        assert len(submitted) == 1
    finally:
        # Completion belongs to the concurrent future even after its request
        # loop has closed. No callback on that dead loop is needed to release.
        submitted[0].set_result(False)
    assert not passwords._capacity.locked()


@pytest.mark.asyncio
async def test_worker_exception_and_submission_failure_release_admission(monkeypatch):
    def broken_check(password: bytes, hashed: bytes) -> bool:
        raise RuntimeError("synthetic worker failure")

    monkeypatch.setattr(passwords.bcrypt, "checkpw", broken_check)
    with pytest.raises(RuntimeError, match="synthetic worker failure"):
        await passwords.check_password("secret", "stored-hash")
    assert not passwords._capacity.locked()

    def failed_submit(work):
        raise RuntimeError("synthetic executor shutdown")

    with monkeypatch.context() as patch:
        patch.setattr(passwords._executor, "submit", failed_submit)
        with pytest.raises(RuntimeError, match="synthetic executor shutdown"):
            await passwords.check_password("secret", "stored-hash")
    assert not passwords._capacity.locked()
    monkeypatch.setattr(passwords.bcrypt, "checkpw", lambda password, hashed: True)
    assert await passwords.check_password("secret", "stored-hash") is True


@pytest.mark.asyncio
async def test_first_unknown_identity_hashes_and_checks_off_loop_once(monkeypatch):
    calls: list[tuple[str, int]] = []
    loop_thread = get_ident()

    def hashpw(password: bytes, salt: bytes) -> bytes:
        calls.append(("hash", get_ident()))
        return b"synthetic-dummy-hash"

    def checkpw(password: bytes, hashed: bytes) -> bool:
        calls.append(("check", get_ident()))
        assert hashed == b"synthetic-dummy-hash"
        return False

    passwords._dummy_password_hash.cache_clear()
    monkeypatch.setattr(passwords.bcrypt, "hashpw", hashpw)
    monkeypatch.setattr(passwords.bcrypt, "checkpw", checkpw)
    try:
        assert await passwords.check_password("wrong", None) is False
        assert await passwords.check_password("wrong", None) is False
        assert [operation for operation, _ in calls] == ["hash", "check", "check"]
        assert all(thread != loop_thread for _, thread in calls)
    finally:
        passwords._dummy_password_hash.cache_clear()
