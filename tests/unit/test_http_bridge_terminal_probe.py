from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.core.clients.proxy_websocket import UpstreamWebSocketMessage
from app.modules.proxy import service as proxy_service
from app.modules.proxy._load_balancer.tunables import RoutingTunables
from app.modules.proxy._service.http_bridge import helpers as bridge_helpers
from app.modules.proxy._service.http_bridge import retry_circuit as circuit_module
from tests.simulation.virtual_time import VirtualClock
from tests.unit.test_proxy_http_bridge import _make_terminal_error_bridge_fixture

pytestmark = pytest.mark.unit


class _ProbeDispatched(Exception):
    pass


async def _admit_dispatched_probe(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any, Any]:
    service, session, request = _make_terminal_error_bridge_fixture(
        request_id="req-terminal-probe",
        key_value="terminal-probe-session",
        response_event_count=0,
    )
    service._clock = VirtualClock(monotonic_value=100.0)
    state = circuit_module._HTTPBridgeRetryCircuitState(
        consecutive_failures=2,
        cooldown_until=99.0,
        last_detail="clean_close",
        last_touched_monotonic=100.0,
    )
    cast(Any, service)._http_bridge_retry_circuits[session.key] = state
    service._durable_bridge = SimpleNamespace(lookup_retry_circuit=AsyncMock(return_value=None))
    session.pending_requests.clear()
    session.queued_request_count = 0
    request.previous_response_id = None
    request.request_text = '{"type":"response.create","model":"gpt-5.4","input":"hello"}'
    request.response_create_attempt_count = 0
    monkeypatch.setattr(service, "_http_bridge_reacquire_snapshot", AsyncMock(return_value=(0, RoutingTunables())))

    async def dispatch_boundary(*_args: Any, **_kwargs: Any) -> None:
        # Keep admission and its finalizer real; stop before opening any
        # upstream connection and model an attempted send for the terminal.
        request.response_create_attempt_count = 1
        raise _ProbeDispatched

    monkeypatch.setattr(service, "_ensure_http_bridge_session_stream_lease_locked", dispatch_boundary)
    with pytest.raises(_ProbeDispatched):
        await service._submit_http_bridge_request(
            session,
            request_state=request,
            text_data=request.request_text,
            queue_limit=8,
        )
    assert state.half_open_until == 700.0
    request.response_id = "resp-terminal-probe"
    request.awaiting_response_created = False
    session.pending_requests.append(request)
    session.queued_request_count = 1
    monkeypatch.setattr(service, "_handle_stream_error", AsyncMock())
    return service, session, request, state


def _failure_frame(response_id: str = "resp-terminal-probe") -> str:
    return json.dumps(
        {
            "type": "response.failed",
            "response": {
                "id": response_id,
                "status": "failed",
                "error": {"code": "server_error", "type": "server_error", "message": "Upstream failed"},
            },
        }
    )


@pytest.mark.asyncio
async def test_matched_server_error_returns_probe_and_admits_one_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    service, session, request, state = await _admit_dispatched_probe(monkeypatch)
    await service._process_http_bridge_upstream_text(session, _failure_frame())

    assert request not in session.pending_requests
    assert state.half_open_until == 0.0
    assert state.consecutive_failures == 2
    assert state.last_detail == "clean_close"
    results = await asyncio.gather(*(service._http_bridge_precreated_retry_allowed(session) for _ in range(3)))
    assert results.count(True) == 1
    assert results.count(False) == 2
    frame = await request.event_queue.get()
    event = json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))
    assert event["response"]["error"]["code"] == "server_error"


@pytest.mark.asyncio
async def test_unmatched_terminal_keeps_live_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    service, session, request, state = await _admit_dispatched_probe(monkeypatch)
    lease_until = state.half_open_until
    await service._process_http_bridge_upstream_text(session, _failure_frame("resp-unrelated"))

    assert request in session.pending_requests
    assert state.half_open_until == lease_until
    assert await service._http_bridge_precreated_retry_allowed(session) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["episode", "failure", "sibling", "durable_epoch", "admission_generation"])
async def test_late_terminal_preserves_newer_episode_or_probe(
    monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    service, session, request, state = await _admit_dispatched_probe(monkeypatch)
    if replacement == "episode":
        state = circuit_module._HTTPBridgeRetryCircuitState(
            consecutive_failures=2,
            cooldown_until=0.0,
            last_detail="clean_close",
            half_open_until=state.half_open_until,
        )
        service._http_bridge_retry_circuits[session.key] = state
    elif replacement == "failure":
        state.consecutive_failures += 1
        state.last_failure_monotonic = 101.0
        state.cooldown_until = 220.0
        state.last_detail = "stream_incomplete"
        state.owed_poison_detail = "stream_incomplete"
    elif replacement == "sibling":
        state.half_open_until = 0.0
        state.cooldown_until = 99.0
        assert await service._http_bridge_precreated_retry_allowed(session) is True
        # Both probes have the same numerical deadline; only claim identity
        # distinguishes the successor from the old request's terminal.
        assert state.half_open_until == 700.0
    elif replacement == "durable_epoch":
        state.persisted_updated_at_epoch = 1.0
    else:
        state.persisted_admission_generation += 1
    before = (state.half_open_until, state.cooldown_until, state.consecutive_failures, state.owed_poison_detail)

    await service._process_http_bridge_upstream_text(session, _failure_frame())

    assert request not in session.pending_requests
    assert (state.half_open_until, state.cooldown_until, state.consecutive_failures, state.owed_poison_detail) == before


@pytest.mark.asyncio
async def test_cancelled_terminal_bookkeeping_returns_ended_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    service, session, request, state = await _admit_dispatched_probe(monkeypatch)
    terminal_claimed = asyncio.Event()

    async def pause_terminal_accounting(*_args: Any, **_kwargs: Any) -> None:
        terminal_claimed.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_http_bridge_effective_anchor_poison_detail", pause_terminal_accounting)
    handler = asyncio.create_task(service._process_http_bridge_upstream_text(session, _failure_frame()))
    try:
        await terminal_claimed.wait()
        assert request not in session.pending_requests
        handler.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handler
    finally:
        if not handler.done():
            handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)

    assert state.half_open_until == 0.0
    assert state.consecutive_failures == 2
    assert await service._http_bridge_precreated_retry_allowed(session) is True
    assert await service._http_bridge_precreated_retry_allowed(session) is False


@pytest.mark.asyncio
async def test_cancellation_during_probe_return_preserves_terminal_and_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, session, request, state = await _admit_dispatched_probe(monkeypatch)
    return_entered = asyncio.Event()
    real_return = service._return_http_bridge_ended_probe
    finalize = AsyncMock(wraps=service._finalize_websocket_request_state)
    monkeypatch.setattr(service, "_finalize_websocket_request_state", finalize)

    async def pause_return(*args: Any) -> None:
        async with service._http_bridge_retry_circuit_lock:
            return_entered.set()
            await allow_return.wait()
        await real_return(*args)

    allow_return = asyncio.Event()
    monkeypatch.setattr(service, "_return_http_bridge_ended_probe", pause_return)
    handler = asyncio.create_task(service._process_http_bridge_upstream_text(session, _failure_frame()))
    try:
        await return_entered.wait()
        handler.cancel()
        allow_return.set()
        with pytest.raises(asyncio.CancelledError):
            await handler
    finally:
        allow_return.set()
        if not handler.done():
            handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)

    assert state.half_open_until == 0.0
    finalize.assert_awaited_once()
    frame = await request.event_queue.get()
    event = json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))
    assert event["response"]["error"]["code"] == "server_error"
    assert await request.event_queue.get() is None


@pytest.mark.asyncio
async def test_internal_retry_terminal_returns_its_new_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    service, session, request, state = await _admit_dispatched_probe(monkeypatch)
    state.half_open_until = 0.0
    state.cooldown_until = 99.0
    original_probe = request.claimed_half_open_probe
    request.response_id = None
    request.awaiting_response_created = True
    request.hard_continuity_anchor = True
    request.account_response_create_lease = SimpleNamespace()
    request.account_response_create_release = AsyncMock()
    send = AsyncMock()
    session.upstream = SimpleNamespace(send_text=send, close=AsyncMock())
    monkeypatch.setattr(service, "_reconnect_http_bridge_session", AsyncMock())
    monkeypatch.setattr(service, "_http_bridge_text_with_account_installation_id", lambda _s, _r, text: text)

    assert await service._retry_http_bridge_precreated_request(session, request_state=request) is True
    send.assert_awaited_once()
    assert request.response_create_attempt_count == 2
    assert request.claimed_half_open_probe is not original_probe
    request.response_id = "resp-terminal-probe"
    request.awaiting_response_created = False

    await service._process_http_bridge_upstream_text(session, _failure_frame())

    assert state.half_open_until == 0.0
    assert state.consecutive_failures == 2
    assert await service._http_bridge_precreated_retry_allowed(session) is True
    assert await service._http_bridge_precreated_retry_allowed(session) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("close_path", ["detach", "direct"])
async def test_cancelled_probe_retirement_admits_one_replacement(
    monkeypatch: pytest.MonkeyPatch, close_path: str
) -> None:
    service, session, request, state = await _admit_dispatched_probe(monkeypatch)
    request.response_event_count = 64

    if close_path == "detach":
        assert await service._detach_http_bridge_request(session, request_state=request) is True
    else:
        await service._close_http_bridge_session(session)

    assert session.closed is True
    session.upstream.close.assert_awaited_once()
    assert session.resource_close_task.done()
    assert not session.pending_requests
    assert state.half_open_until == 0.0
    assert state.consecutive_failures == 2
    assert state.last_detail == "clean_close"
    results = await asyncio.gather(*(service._http_bridge_precreated_retry_allowed(session) for _ in range(3)))
    assert results.count(True) == 1
    assert results.count(False) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["timeout", "cancellation"])
async def test_retired_probe_waits_for_owned_close_after_bounded_interruption(
    monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    service, session, request, state = await _admit_dispatched_probe(monkeypatch)
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    async def blocked_close() -> None:
        close_started.set()
        await allow_close.wait()

    session.upstream.close = blocked_close
    monkeypatch.setattr(proxy_service, "_HTTP_BRIDGE_BACKGROUND_CLOSE_TIMEOUT_SECONDS", 0.01)
    detach = asyncio.create_task(service._detach_http_bridge_request(session, request_state=request))
    try:
        await close_started.wait()
        if interruption == "cancellation":
            detach.cancel()
            with pytest.raises(asyncio.CancelledError):
                await detach
        else:
            assert await detach is True
        assert service._background_cleanup_tasks
        assert not session.resource_close_task.done()
        assert state.half_open_until == 700.0
        assert await service._http_bridge_precreated_retry_allowed(session) is False

        allow_close.set()
        await session.resource_close_task
        await asyncio.gather(*service._background_cleanup_tasks)
        assert state.half_open_until == 0.0
        assert await service._http_bridge_precreated_retry_allowed(session) is True
        assert await service._http_bridge_precreated_retry_allowed(session) is False
    finally:
        allow_close.set()
        await asyncio.gather(detach, return_exceptions=True)
        if session.resource_close_task is not None:
            await asyncio.gather(session.resource_close_task, return_exceptions=True)
        await asyncio.gather(*service._background_cleanup_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_active_sibling_keeps_cancelled_probe_until_retirement(monkeypatch: pytest.MonkeyPatch) -> None:
    service, session, request, state = await _admit_dispatched_probe(monkeypatch)
    _, _, sibling = _make_terminal_error_bridge_fixture(
        request_id="req-active-sibling", key_value="unused", response_event_count=1
    )
    session.pending_requests.append(sibling)
    session.queued_request_count += 1

    assert await service._detach_http_bridge_request(session, request_state=request) is True
    session.upstream.close.assert_not_awaited()
    assert state.half_open_until == 700.0
    assert await service._http_bridge_precreated_retry_allowed(session) is False

    assert await service._detach_http_bridge_request(session, request_state=sibling) is True
    session.upstream.close.assert_awaited_once()
    assert state.half_open_until == 0.0


@pytest.mark.asyncio
async def test_sibling_terminal_reader_exit_returns_cancelled_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    service, session, request, state = await _admit_dispatched_probe(monkeypatch)
    _, _, sibling = _make_terminal_error_bridge_fixture(
        request_id="req-active-sibling", key_value="unused", response_event_count=1
    )
    sibling.response_id = "resp-sibling"
    sibling.awaiting_response_created = False
    session.pending_requests.append(sibling)
    session.queued_request_count += 1
    session.upstream = SimpleNamespace(
        close=AsyncMock(),
        receive=AsyncMock(return_value=UpstreamWebSocketMessage(kind="text", text=_failure_frame("resp-sibling"))),
    )
    assert await service._detach_http_bridge_request(session, request_state=request) is True
    assert state.half_open_until == 700.0

    reader = asyncio.create_task(service._relay_http_bridge_upstream_messages(session))
    session.upstream_reader = reader
    try:
        await reader
        # Reader completion callbacks transfer any remaining claim return to
        # the service's existing cleanup registry before this turn resumes.
        await asyncio.sleep(0)
        await asyncio.gather(*service._background_cleanup_tasks)
        session.upstream.close.assert_awaited_once()
        assert not session.pending_requests
        assert state.half_open_until == 0.0
        assert state.consecutive_failures == 2
        results = await asyncio.gather(*(service._http_bridge_precreated_retry_allowed(session) for _ in range(3)))
        assert results.count(True) == 1
        assert results.count(False) == 2
    finally:
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await asyncio.gather(*service._background_cleanup_tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete", ["socket_failure", "live_reader"])
async def test_incomplete_transport_close_keeps_retired_probe(monkeypatch: pytest.MonkeyPatch, incomplete: str) -> None:
    service, session, request, state = await _admit_dispatched_probe(monkeypatch)
    allow_reader_exit = asyncio.Event()
    reader = None
    if incomplete == "socket_failure":
        session.upstream.close.side_effect = RuntimeError("socket close failed")
    else:
        reader_started = asyncio.Event()

        async def cancellation_resistant_reader() -> None:
            reader_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await allow_reader_exit.wait()

        reader = asyncio.create_task(cancellation_resistant_reader())
        session.upstream_reader = reader
        await reader_started.wait()
        real_cancel = bridge_helpers._await_cancelled_task

        async def bounded_cancel(task: asyncio.Task[Any], **kwargs: Any) -> bool:
            return await real_cancel(task, timeout_seconds=0.0, **kwargs)

        monkeypatch.setattr(bridge_helpers, "_await_cancelled_task", bounded_cancel)
    try:
        assert await service._detach_http_bridge_request(session, request_state=request) is True
        assert session.resource_close_task.done()
        assert state.half_open_until == 700.0
        assert await service._http_bridge_precreated_retry_allowed(session) is False
        if reader is not None:
            assert not reader.done()
    finally:
        allow_reader_exit.set()
        if reader is not None:
            await asyncio.gather(reader, return_exceptions=True)
        await asyncio.gather(*service._background_cleanup_tasks, return_exceptions=True)
    if reader is not None:
        assert state.half_open_until == 0.0
        assert await service._http_bridge_precreated_retry_allowed(session) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["episode", "sibling", "reused_request"])
async def test_retired_probe_cannot_return_newer_claim(monkeypatch: pytest.MonkeyPatch, replacement: str) -> None:
    service, session, request, state = await _admit_dispatched_probe(monkeypatch)
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    async def blocked_close() -> None:
        close_started.set()
        await allow_close.wait()

    session.upstream.close = blocked_close
    detach = asyncio.create_task(service._detach_http_bridge_request(session, request_state=request))
    try:
        await close_started.wait()
        if replacement == "episode":
            state.persisted_admission_generation += 1
        else:
            state.half_open_until = 0.0
            state.cooldown_until = 99.0
            claims: list[Any] = []
            assert await service._http_bridge_precreated_retry_allowed(session, claimed_probe_out=claims) is True
            if replacement == "reused_request":
                request.claimed_half_open_probe = claims[0]
        before = (state.half_open_probe, state.cooldown_until, state.consecutive_failures)
        allow_close.set()
        assert await detach is True
        assert state.half_open_until == 700.0
        assert (state.half_open_probe, state.cooldown_until, state.consecutive_failures) == before
        assert await service._http_bridge_precreated_retry_allowed(session) is False
    finally:
        allow_close.set()
        await asyncio.gather(detach, return_exceptions=True)
        if session.resource_close_task is not None:
            await asyncio.gather(session.resource_close_task, return_exceptions=True)
