from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache, partial
from threading import Lock

import bcrypt

from app.core.exceptions import DashboardRateLimitError

# One CPU-bound password job per process, with no waiting queue. The app supports
# one server worker per instance; concurrent sign-ins can retry instead of
# accumulating bcrypt work that competes with the inference event loop.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="password")
_capacity = Lock()


async def _run_password_work[T](work: Callable[[], T]) -> T:
    if not _capacity.acquire(blocking=False):
        raise DashboardRateLimitError(
            "Password verification is busy. Try again shortly.",
            retry_after=1,
            code="password_work_busy",
        )
    try:
        worker = _executor.submit(work)
    except BaseException:
        _capacity.release()
        raise
    # The concurrent future owns admission, not the request or its event loop.
    # Cancellation and loop shutdown must not admit another job while this
    # thread is still hashing. This callback also handles cancellation before
    # the executor starts the work.
    worker.add_done_callback(lambda _: _capacity.release())
    result = asyncio.wrap_future(worker)
    result.add_done_callback(_consume_result)
    return await asyncio.shield(result)


def _consume_result[T](result: asyncio.Future[T]) -> None:
    # A cancelled request no longer observes a worker failure. Retrieve it
    # without logging its arguments (which contain password material).
    if not result.cancelled():
        result.exception()


def hash_password(password: str) -> str:
    """Synchronous hash for the standalone host-recovery CLI."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


async def hash_password_async(password: str) -> str:
    return await _run_password_work(partial(hash_password, password))


async def check_password(password: str, password_hash: str | None) -> bool:
    return await _run_password_work(partial(_verify_password, password, password_hash))


def _verify_password(password: str, password_hash: str | None) -> bool:
    # Keep first-use dummy generation inside the same admitted worker as the
    # comparison. Unknown identities still perform exactly one verification.
    return _check_password(password, password_hash if password_hash is not None else _dummy_password_hash())


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    return hash_password(secrets.token_urlsafe(32))


def _check_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False
