from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Literal

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient, Response

import app.modules.proxy.service as proxy_module
from app.core.crypto import TokenEncryptor
from app.core.types import JsonValue
from app.core.utils.time import naive_utc_to_epoch, utcnow
from app.db.models import Account, AccountStatus, StickySessionKind
from app.db.session import SessionLocal
from app.modules.accounts.repository import AccountsRepository
from app.modules.api_keys.repository import ApiKeysRepository
from app.modules.api_keys.service import ApiKeyCreateData, ApiKeysService
from app.modules.proxy.affinity import _codex_backend_identity, _codex_session_selection_key
from app.modules.proxy.durable_bridge_coordinator import DurableBridgeSessionCoordinator
from app.modules.proxy.durable_bridge_repository import DurableBridgeAliasRegistration
from app.modules.proxy.file_pin_repository import FileAccountPinRepository
from app.modules.proxy.sticky_repository import StickySessionsRepository
from app.modules.usage.repository import UsageRepository
from tests.integration.test_proxy_affinity_websocket_observation import SyntheticUpstream

pytestmark = pytest.mark.integration


@pytest.fixture
def upstream_accounts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    async def fake_stream(payload, headers, access_token, account_id, **kwargs) -> AsyncIterator[str]:
        del payload, headers, access_token, kwargs
        seen.append(account_id)
        yield (
            "data: "
            + json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": f"resp_allowance_{len(seen)}",
                        "object": "response",
                        "status": "completed",
                        "output": [],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    },
                }
            )
            + "\n\n"
        )

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    return seen


async def _configure_routing(client: AsyncClient, *, api_key_auth_enabled: bool = False) -> None:
    response = await client.put(
        "/api/settings",
        json={
            "routingStrategy": "capacity_weighted",
            "preferEarlierResetAccounts": True,
            "stickyThreadsEnabled": False,
            "stickyReallocationBudgetThresholdPct": 100.0,
            "apiKeyAuthEnabled": api_key_auth_enabled,
        },
    )
    assert response.status_code == 200


async def _seed_account(
    account_id: str,
    *,
    weekly_used: float,
    reset_days: int,
    status: AccountStatus = AccountStatus.ACTIVE,
) -> None:
    now = utcnow()
    now_epoch = int(naive_utc_to_epoch(now))
    encryptor = TokenEncryptor()
    async with SessionLocal() as session:
        await AccountsRepository(session).upsert(
            Account(
                id=account_id,
                chatgpt_account_id=account_id,
                email=f"{account_id}@example.com",
                plan_type="plus",
                access_token_encrypted=encryptor.encrypt("synthetic-access-token"),
                refresh_token_encrypted=encryptor.encrypt("synthetic-refresh-token"),
                id_token_encrypted=encryptor.encrypt("synthetic-id-token"),
                last_refresh=now,
                status=status,
            )
        )
        usage = UsageRepository(session)
        for window, used_percent, window_minutes, reset_at in (
            ("primary", 10.0, 300, now_epoch + 3600),
            ("secondary", weekly_used, 10080, now_epoch + reset_days * 86400),
        ):
            await usage.add_entry(
                account_id=account_id,
                used_percent=used_percent,
                window=window,
                window_minutes=window_minutes,
                reset_at=reset_at,
                recorded_at=now,
                credits_has=True,
                credits_unlimited=False,
                credits_balance=100.0,
            )


async def _send_response(
    client: AsyncClient,
    *,
    cache_key: str,
    headers: dict[str, str] | None = None,
    file_id: str | None = None,
    path: str = "/v1/responses",
    extra_payload: dict[str, JsonValue] | None = None,
) -> Response:
    content = [{"type": "input_text", "text": "Continue this synthetic request."}]
    if file_id is not None:
        content.append({"type": "input_file", "file_id": file_id})
    response = await client.post(
        path,
        headers=headers,
        json={
            "model": "gpt-5.1",
            "instructions": "Respond briefly.",
            "input": [{"role": "user", "content": content}],
            "prompt_cache_key": cache_key,
            "stream": True,
            **(extra_payload or {}),
        },
    )
    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert any(event.get("type") == "response.completed" for event in events)
    assert not any(event.get("type") == "response.failed" for event in events)
    return response


@pytest.mark.asyncio
async def test_fresh_request_prefers_included_allowance_before_earlier_reset(async_client, upstream_accounts):
    await _configure_routing(async_client)
    await _seed_account("acc_credit_early", weekly_used=100.0, reset_days=1)
    # Both accounts are in the same drain tier, so health preference cannot
    # hide the earlier-reset regression. The later account still has allowance.
    await _seed_account("acc_included_later", weekly_used=95.0, reset_days=6)

    await _send_response(async_client, cache_key="fresh-subscription-first")

    assert upstream_accounts == ["acc_included_later"]
    async with SessionLocal() as session:
        assert (
            await StickySessionsRepository(session).get_account_id(
                "fresh-subscription-first", kind=StickySessionKind.PROMPT_CACHE
            )
            == "acc_included_later"
        )


@pytest.mark.asyncio
async def test_existing_prompt_cache_owner_keeps_credits_after_allowance_exhaustion(async_client, upstream_accounts):
    await _configure_routing(async_client)
    await _seed_account("acc_existing_owner", weekly_used=20.0, reset_days=1)
    await _seed_account("acc_unused_allowance", weekly_used=20.0, reset_days=6)

    await _send_response(async_client, cache_key="existing-conversation")
    assert upstream_accounts == ["acc_existing_owner"]

    async with SessionLocal() as session:
        await UsageRepository(session).add_entry(
            account_id="acc_existing_owner",
            used_percent=100.0,
            window="secondary",
            window_minutes=10080,
            reset_at=int(naive_utc_to_epoch(utcnow())) + 86400,
            credits_has=True,
            credits_unlimited=False,
            credits_balance=100.0,
        )

    await _send_response(async_client, cache_key="existing-conversation")
    await _send_response(async_client, cache_key="existing-conversation")
    await _send_response(async_client, cache_key="new-conversation-after-exhaustion")

    assert upstream_accounts == [
        "acc_existing_owner",
        "acc_existing_owner",
        "acc_existing_owner",
        "acc_unused_allowance",
    ]
    async with SessionLocal() as session:
        assert (
            await StickySessionsRepository(session).get_account_id(
                "existing-conversation", kind=StickySessionKind.PROMPT_CACHE
            )
            == "acc_existing_owner"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", ["file", "turn_state"])
@pytest.mark.parametrize("child", [False, True])
async def test_account_owned_request_stays_on_exhausted_owner(
    async_client, upstream_accounts, ownership: Literal["file", "turn_state"], child: bool
):
    await _configure_routing(async_client)
    await _seed_account("acc_hard_owner", weekly_used=100.0, reset_days=1)
    await _seed_account("acc_other_allowance", weekly_used=20.0, reset_days=6)
    async with SessionLocal() as session:
        if ownership == "file":
            await FileAccountPinRepository(session).claim("file_owned", "acc_hard_owner", ttl_seconds=300)
        else:
            await StickySessionsRepository(session).upsert(
                "owned-turn-state", "acc_hard_owner", kind=StickySessionKind.CODEX_SESSION
            )

    if ownership == "turn_state":
        # Opaque turn-state tokens require a registered, API-key-scoped owner;
        # a legacy sticky row alone does not prove upstream token ownership.
        coordinator = DurableBridgeSessionCoordinator(SessionLocal)
        owner = await coordinator.claim_live_session(
            session_key_kind="prompt_cache",
            session_key_value="ownership-is-not-a-routing-preference",
            api_key_id=None,
            instance_id="subscription-routing-test",
            owner_process_epoch="test-process",
            lease_ttl_seconds=300.0,
            account_id="acc_hard_owner",
            model="gpt-5.1",
            service_tier=None,
            latest_turn_state="owned-turn-state",
            latest_response_id=None,
            allow_takeover=True,
        )
        registration = await coordinator.register_turn_state(
            session_id=owner.session_id,
            api_key_id=None,
            instance_id="subscription-routing-test",
            owner_epoch=owner.owner_epoch,
            turn_state="owned-turn-state",
            lease_ttl_seconds=300.0,
        )
        assert registration == DurableBridgeAliasRegistration.REGISTERED

    headers = _child_headers("owned-child") if child else {}
    if ownership == "turn_state":
        headers["x-codex-turn-state"] = "owned-turn-state"
    for _ in range(2):
        await _send_response(
            async_client,
            cache_key="ownership-is-not-a-routing-preference",
            headers=headers,
            file_id="file_owned" if ownership == "file" else None,
            path="/backend-api/codex/responses" if ownership == "turn_state" or child else "/v1/responses",
        )

    assert upstream_accounts == ["acc_hard_owner", "acc_hard_owner"]
    async with SessionLocal() as session:
        if ownership == "file":
            assert await FileAccountPinRepository(session).get_live_account_id("file_owned") == "acc_hard_owner"
        else:
            assert (
                await StickySessionsRepository(session).get_account_id(
                    "owned-turn-state", kind=StickySessionKind.CODEX_SESSION
                )
                == "acc_hard_owner"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("alternative", ["exhausted", "paused"])
async def test_fresh_request_keeps_credit_fallback_without_eligible_allowance(
    async_client, upstream_accounts, alternative: Literal["exhausted", "paused"]
):
    await _configure_routing(async_client)
    await _seed_account("acc_credit_fallback", weekly_used=100.0, reset_days=1)
    await _seed_account(
        "acc_unavailable_alternative",
        weekly_used=100.0 if alternative == "exhausted" else 20.0,
        reset_days=6,
        status=AccountStatus.PAUSED if alternative == "paused" else AccountStatus.ACTIVE,
    )

    await _send_response(async_client, cache_key="fresh-credit-fallback")

    assert upstream_accounts == ["acc_credit_fallback"]


@pytest.mark.asyncio
async def test_included_allowance_does_not_escape_api_key_account_scope(async_client, upstream_accounts):
    await _configure_routing(async_client, api_key_auth_enabled=True)
    await _seed_account("acc_assigned_credit", weekly_used=100.0, reset_days=1)
    await _seed_account("acc_unassigned_allowance", weekly_used=20.0, reset_days=6)
    async with SessionLocal() as session:
        key = await ApiKeysService(ApiKeysRepository(session)).create_key(
            ApiKeyCreateData(
                name="subscription routing scope",
                allowed_models=None,
                assigned_account_ids=["acc_assigned_credit"],
            )
        )

    await _send_response(
        async_client,
        cache_key="scoped-fresh-conversation",
        headers={"Authorization": f"Bearer {key.key}"},
    )

    assert upstream_accounts == ["acc_assigned_credit"]


def _child_headers(thread_id: str) -> dict[str, str]:
    return {
        "session-id": "child-placement-process",
        "thread-id": thread_id,
        "x-openai-subagent": "collab_spawn",
        "x-codex-parent-thread-id": "child-placement-parent",
    }


async def _seed_process_preference(account_id: str, *, legacy: bool = False) -> None:
    async with SessionLocal() as session:
        await StickySessionsRepository(session).upsert(
            "child-placement-process" if legacy else _codex_session_selection_key("child-placement-process"),
            account_id,
            kind=StickySessionKind.CODEX_SESSION,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_source", ["headers", "body"])
async def test_independent_children_use_allowance_then_keep_their_own_accounts(
    async_client, upstream_accounts, metadata_source: str
):
    await _configure_routing(async_client)
    await _seed_account("acc_parent_credit", weekly_used=100.0, reset_days=1)
    await _seed_account("acc_child_first", weekly_used=20.0, reset_days=1)
    await _seed_account("acc_child_next", weekly_used=20.0, reset_days=6)
    await _seed_process_preference("acc_parent_credit")

    async def send_child(thread_id: str) -> None:
        headers = _child_headers(thread_id)
        metadata: dict[str, JsonValue] = {}
        if metadata_source == "body":
            metadata = {
                "client_metadata": {
                    "x-openai-subagent": headers.pop("x-openai-subagent"),
                    "x-codex-parent-thread-id": headers.pop("x-codex-parent-thread-id"),
                }
            }
        await _send_response(
            async_client,
            cache_key="shared-upstream-cache-hint",
            headers=headers,
            path="/backend-api/codex/responses",
            extra_payload=metadata,
        )

    await send_child("child-one")
    assert upstream_accounts == ["acc_child_first"]
    async with SessionLocal() as session:
        await UsageRepository(session).add_entry(
            account_id="acc_child_first",
            used_percent=100.0,
            window="secondary",
            window_minutes=10080,
            reset_at=int(naive_utc_to_epoch(utcnow())) + 86400,
            credits_has=True,
            credits_unlimited=False,
            credits_balance=100.0,
        )

    await send_child("child-two")
    await send_child("child-one")
    await _send_response(
        async_client,
        cache_key="shared-upstream-cache-hint",
        headers={"session-id": "child-placement-process", "thread-id": "child-placement-parent"},
        path="/backend-api/codex/responses",
    )
    assert upstream_accounts == ["acc_child_first", "acc_child_next", "acc_child_first", "acc_parent_credit"]
    async with SessionLocal() as session:
        repo = StickySessionsRepository(session)
        for thread_id, account_id in (("child-one", "acc_child_first"), ("child-two", "acc_child_next")):
            thread_key = _codex_backend_identity(_child_headers(thread_id)).thread_selection_key
            assert thread_key is not None
            assert await repo.get_account_id(thread_key, kind=StickySessionKind.PROMPT_CACHE) == account_id
        assert (
            await repo.get_account_id(
                _codex_session_selection_key("child-placement-process"), kind=StickySessionKind.CODEX_SESSION
            )
            == "acc_parent_credit"
        )


@pytest.mark.asyncio
async def test_independent_child_uses_reset_preference_instead_of_healthy_parent(async_client, upstream_accounts):
    await _configure_routing(async_client)
    await _seed_account("acc_healthy_parent", weekly_used=20.0, reset_days=6)
    await _seed_account("acc_earlier_reset", weekly_used=20.0, reset_days=1)
    await _seed_process_preference("acc_healthy_parent")

    await _send_response(
        async_client,
        cache_key="healthy-parent-cache",
        headers=_child_headers("independent-child"),
        path="/backend-api/codex/responses",
    )

    assert upstream_accounts == ["acc_earlier_reset"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_independent_child_does_not_create_or_overwrite_process_preference(
    async_client, upstream_accounts, legacy: bool
):
    await _configure_routing(async_client)
    await _seed_account("acc_legacy_owner", weekly_used=100.0, reset_days=1)
    await _seed_account("acc_child_allowance", weekly_used=20.0, reset_days=6)
    if legacy:
        await _seed_process_preference("acc_legacy_owner", legacy=True)

    await _send_response(
        async_client,
        cache_key="legacy-child-cache",
        headers=_child_headers("new-child"),
        path="/backend-api/codex/responses",
    )

    assert upstream_accounts == ["acc_legacy_owner" if legacy else "acc_child_allowance"]
    async with SessionLocal() as session:
        repo = StickySessionsRepository(session)
        assert (
            await repo.get_account_id(
                _codex_session_selection_key("child-placement-process"), kind=StickySessionKind.CODEX_SESSION
            )
            is None
        )
        assert await repo.get_account_id("child-placement-process", kind=StickySessionKind.CODEX_SESSION) == (
            "acc_legacy_owner" if legacy else None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "input_item",
    [
        {"type": "agent_message", "text": "Opaque parent state"},
        {"type": "reasoning", "encrypted_content": "owner-bound"},
    ],
)
async def test_nonportable_child_keeps_process_preference(async_client, upstream_accounts, input_item):
    await _configure_routing(async_client)
    await _seed_account("acc_parent_credit", weekly_used=100.0, reset_days=1)
    await _seed_account("acc_other_allowance", weekly_used=20.0, reset_days=6)
    await _seed_process_preference("acc_parent_credit")

    await _send_response(
        async_client,
        cache_key="nonportable-child-cache",
        headers=_child_headers("nonportable-child"),
        path="/backend-api/codex/responses",
        extra_payload={"input": [input_item]},
    )

    assert upstream_accounts == ["acc_parent_credit"]


async def _seed_websocket_child_accounts(*, legacy_owner: bool) -> str:
    await _seed_account("acc_ws_parent", weekly_used=100.0, reset_days=1)
    await _seed_account("acc_ws_child", weekly_used=20.0, reset_days=1)
    await _seed_account("acc_ws_spare", weekly_used=20.0, reset_days=6)
    await _seed_process_preference("acc_ws_parent")
    if legacy_owner:
        await _seed_process_preference("acc_ws_parent", legacy=True)
    async with SessionLocal() as session:
        key = await ApiKeysService(ApiKeysRepository(session)).create_key(
            ApiKeyCreateData(name="child-websocket-routing", allowed_models=None)
        )
        return key.key


async def _exhaust_websocket_child() -> None:
    async with SessionLocal() as session:
        await UsageRepository(session).add_entry(
            account_id="acc_ws_child",
            used_percent=100.0,
            window="secondary",
            window_minutes=10080,
            reset_at=int(naive_utc_to_epoch(utcnow())) + 86400,
            credits_has=True,
            credits_unlimited=False,
            credits_balance=100.0,
        )


@pytest.mark.parametrize("dependency", ["independent", "legacy_owner", "opaque_history"])
def test_direct_websocket_child_placement_preserves_its_admitted_owner(app_instance, monkeypatch, dependency):
    sent_accounts: list[str] = []

    class ChildUpstream(SyntheticUpstream):
        def __init__(self, account_id: str) -> None:
            super().__init__()
            self.account_id = account_id

        async def send_text(self, text: str) -> None:
            sent_accounts.append(self.account_id)
            await super().send_text(text)

    async def connect(headers, access_token, account_id, **kwargs):
        del headers, access_token, kwargs
        return ChildUpstream(account_id)

    async def seed() -> str:
        return await _seed_websocket_child_accounts(legacy_owner=dependency == "legacy_owner")

    monkeypatch.setattr(proxy_module, "connect_responses_websocket", connect)
    with TestClient(app_instance, client=("127.0.0.1", 50000)) as client:
        assert client.portal is not None
        key = client.portal.call(seed)
        settings_response = client.put(
            "http://localhost/api/settings",
            json={
                "routingStrategy": "capacity_weighted",
                "preferEarlierResetAccounts": True,
                "stickyThreadsEnabled": False,
                "stickyReallocationBudgetThresholdPct": 100.0,
                "apiKeyAuthEnabled": True,
            },
        )
        assert settings_response.status_code == 200
        headers = {"authorization": f"Bearer {key}", **_child_headers("websocket-child")}
        expected_account = "acc_ws_child" if dependency == "independent" else "acc_ws_parent"
        input_items = [{"role": "user", "content": [{"type": "input_text", "text": "Independent task"}]}]
        if dependency == "opaque_history":
            input_items.insert(0, {"type": "agent_message", "text": "Opaque inherited parent state"})
        payload = {
            "type": "response.create",
            "model": "gpt-5.1",
            "instructions": "Complete this synthetic task.",
            "input": input_items,
            "prompt_cache_key": "shared-upstream-cache-hint",
        }
        with client.websocket_connect("ws://localhost/backend-api/codex/responses", headers=headers) as websocket:
            websocket.send_json(payload)
            assert websocket.receive_json()["type"] == "response.created"
            assert websocket.receive_json()["type"] == "response.completed"
            assert sent_accounts == [expected_account]
            client.portal.call(_exhaust_websocket_child)
            # A later response on the same connection must keep its admitted
            # owner even though an unused account still has included allowance.
            websocket.send_json(payload)
            assert websocket.receive_json()["type"] == "response.created"
            assert websocket.receive_json()["type"] == "response.completed"
            assert sent_accounts == [expected_account, expected_account]
        assert client.portal.call(app_instance.state.proxy_service.drain_persistence_tasks, 5.0)
