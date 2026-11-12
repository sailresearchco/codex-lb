from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Literal

import pytest
from httpx import AsyncClient, Response

import app.modules.proxy.service as proxy_module
from app.core.crypto import TokenEncryptor
from app.core.utils.time import naive_utc_to_epoch, utcnow
from app.db.models import Account, AccountStatus, StickySessionKind
from app.db.session import SessionLocal
from app.modules.accounts.repository import AccountsRepository
from app.modules.api_keys.repository import ApiKeysRepository
from app.modules.api_keys.service import ApiKeyCreateData, ApiKeysService
from app.modules.proxy.durable_bridge_coordinator import DurableBridgeSessionCoordinator
from app.modules.proxy.durable_bridge_repository import DurableBridgeAliasRegistration
from app.modules.proxy.file_pin_repository import FileAccountPinRepository
from app.modules.proxy.sticky_repository import StickySessionsRepository
from app.modules.usage.repository import UsageRepository

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
async def test_account_owned_request_stays_on_exhausted_owner(
    async_client, upstream_accounts, ownership: Literal["file", "turn_state"]
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

    for _ in range(2):
        await _send_response(
            async_client,
            cache_key="ownership-is-not-a-routing-preference",
            headers={"x-codex-turn-state": "owned-turn-state"} if ownership == "turn_state" else None,
            file_id="file_owned" if ownership == "file" else None,
            path="/backend-api/codex/responses" if ownership == "turn_state" else "/v1/responses",
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
