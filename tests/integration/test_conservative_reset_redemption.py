from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.clients.rate_limit_reset_credits import (
    ConsumeResetCreditError,
    ConsumeResetCreditResponse,
    ResetCreditsResponse,
    build_snapshot,
)
from app.core.clients.usage import UsageFetchError
from app.core.crypto import TokenEncryptor
from app.core.usage import reset_credits_refresh_scheduler as scheduler
from app.core.usage.models import UsagePayload
from app.db.models import Account, AccountStatus, ResetCreditRedeemRequest
from app.db.session import SessionLocal
from app.modules.accounts.repository import AccountsRepository
from app.modules.rate_limit_reset_credits import api as reset_api
from app.modules.rate_limit_reset_credits import redeem_coordination as coordination
from app.modules.rate_limit_reset_credits.store import RateLimitResetCreditsStore
from app.modules.usage import updater as usage_updater
from app.modules.usage.repository import UsageRepository

pytestmark = pytest.mark.integration


@dataclass
class Upstream:
    now: datetime = field(default_factory=lambda: datetime.now(UTC).replace(microsecond=0))
    used: float = 100.0
    reset_at: int = 0
    expires_at: datetime | None = None
    credits: list[str] = field(default_factory=lambda: ["coupon-1", "coupon-2", "coupon-3"])
    consumed: list[str] = field(default_factory=list)
    window: str = "secondary_window"
    report_weekly: bool = True
    fail_consume: bool = False
    included_served: float = 0.0
    paid_served: float = 0.0
    natural_refills: int = 0

    def __post_init__(self):
        self.reset_at = int((self.now + timedelta(days=5)).timestamp())
        self.expires_at = self.now + timedelta(days=17)

    async def fetch_credits(self, *args, **kwargs):
        return ResetCreditsResponse.model_validate(
            {
                "available_count": len(self.credits),
                "credits": [
                    {"id": credit, "status": "available", "expires_at": self.expires_at} for credit in self.credits
                ],
            }
        )

    async def fetch_usage(self, **kwargs):
        return UsagePayload.model_validate(
            {
                "plan_type": "pro",
                "rate_limit": {
                    self.window: {
                        "used_percent": self.used,
                        "reset_at": self.reset_at,
                        "limit_window_seconds": 604800,
                    }
                }
                if self.report_weekly
                else None,
                "additional_rate_limits": [],
            }
        )

    async def consume(self, access_token, account_id, credit_id, **kwargs):
        self.consumed.append(credit_id)
        if self.fail_consume:
            raise ConsumeResetCreditError(503, "ambiguous upstream response")
        self.credits.remove(credit_id)
        self.used = 0.0
        self.reset_at = int((self.now + timedelta(days=7)).timestamp())
        return ConsumeResetCreditResponse.model_validate(
            {"code": "reset", "windows_reset": 1, "credit": {"id": credit_id, "redeemed_at": self.now}}
        )

    def serve(self, percent: float):
        included = min(percent, 100.0 - self.used)
        self.included_served += included
        self.paid_served += percent - included
        self.used = min(100.0, self.used + percent)

    def advance(self, duration: timedelta):
        target = self.now + duration
        while self.reset_at <= int(target.timestamp()):
            self.now = datetime.fromtimestamp(self.reset_at, UTC)
            self.used = 0.0
            self.reset_at += 7 * 86400
            self.natural_refills += 1
        self.now = target


async def _setup(monkeypatch, *, account_id="early-account"):
    upstream = Upstream()
    encryptor = TokenEncryptor()
    account = Account(
        id=account_id,
        chatgpt_account_id=account_id,
        email=f"{account_id}@example.invalid",
        plan_type="pro",
        access_token_encrypted=encryptor.encrypt("synthetic-access-token"),
        refresh_token_encrypted=encryptor.encrypt("synthetic-refresh-token"),
        id_token_encrypted=encryptor.encrypt("synthetic-id-token"),
        last_refresh=upstream.now.replace(tzinfo=None),
        status=AccountStatus.ACTIVE,
    )
    async with SessionLocal() as session:
        await AccountsRepository(session).upsert(account)
    await _write_candidate(account, upstream)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return upstream.now.astimezone(tz) if tz else upstream.now.replace(tzinfo=None)

    monkeypatch.setattr(scheduler, "datetime", Clock)
    monkeypatch.setattr(coordination, "datetime", Clock)
    monkeypatch.setattr(reset_api, "datetime", Clock)
    monkeypatch.setattr(reset_api, "consume_reset_credit", upstream.consume)
    monkeypatch.setattr(usage_updater, "fetch_usage", upstream.fetch_usage)
    return account, upstream


async def _write_candidate(account: Account, upstream: Upstream):
    async with SessionLocal() as session:
        await UsageRepository(session).add_entry(
            account_id=account.id,
            used_percent=upstream.used,
            window="primary" if upstream.window == "primary_window" else "secondary",
            window_minutes=10080,
            reset_at=upstream.reset_at,
        )


async def _tick(account: Account, upstream: Upstream, store=None):
    await scheduler.refresh_reset_credits_for_accounts(
        accounts=[account],
        encryptor=TokenEncryptor(),
        store=store or RateLimitResetCreditsStore(),
        fetch_fn=upstream.fetch_credits,
        auto_redeem_before_expiry=True,
        auto_redeem_window_seconds=300,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["primary_window", "secondary_window"])
async def test_early_redemption_uses_fresh_weekly_window(async_client, monkeypatch, window):
    account, upstream = await _setup(monkeypatch)
    upstream.window = window
    upstream.used = 99.0
    upstream.reset_at = int((upstream.now + timedelta(hours=72)).timestamp())
    await _write_candidate(account, upstream)
    await _tick(account, upstream)
    assert upstream.consumed == ["coupon-1"]
    assert upstream.used == 0
    await _tick(account, upstream)
    assert upstream.consumed == ["coupon-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_state", ["manual_reset", "missing_weekly", "near_refresh", "fetch_failure"])
async def test_stale_candidate_cannot_spend_a_coupon(async_client, monkeypatch, fresh_state):
    account, upstream = await _setup(monkeypatch)
    if fresh_state == "manual_reset":
        upstream.used = 0.0
    elif fresh_state == "missing_weekly":
        upstream.report_weekly = False
    elif fresh_state == "near_refresh":
        upstream.reset_at = int((upstream.now + timedelta(hours=71)).timestamp())
    else:

        async def failed_fetch(**kwargs):
            raise UsageFetchError(503, "synthetic fetch failure")

        monkeypatch.setattr(usage_updater, "fetch_usage", failed_fetch)
    await _tick(account, upstream)
    assert upstream.consumed == []
    async with SessionLocal() as session:
        assert (
            await session.scalar(
                select(ResetCreditRedeemRequest).where(ResetCreditRedeemRequest.account_id == account.id)
            )
            is None
        )


@pytest.mark.asyncio
async def test_concurrent_replicas_consume_only_one_coupon(async_client, monkeypatch):
    account, upstream = await _setup(monkeypatch)
    await asyncio.gather(
        _tick(account, upstream, RateLimitResetCreditsStore()),
        _tick(account, upstream, RateLimitResetCreditsStore()),
    )
    assert upstream.consumed == ["coupon-1"]


@pytest.mark.asyncio
async def test_manual_consume_wins_before_queued_automatic_recheck(async_client, monkeypatch):
    account, upstream = await _setup(monkeypatch)
    store = RateLimitResetCreditsStore()
    await store.set(account.id, build_snapshot(await upstream.fetch_credits()))
    consuming = asyncio.Event()
    release = asyncio.Event()
    automatic_started = asyncio.Event()
    original_auto = scheduler._auto_redeem_reset_credit

    async def delayed_consume(*args, **kwargs):
        consuming.set()
        await release.wait()
        return await upstream.consume(*args, **kwargs)

    async def started_auto(*args, **kwargs):
        automatic_started.set()
        return await original_auto(*args, **kwargs)

    async def manual_reset():
        async with SessionLocal() as session:
            current = await session.get(Account, account.id)
            assert current is not None
            await reset_api._redeem_soonest_reset_credit(
                account=current,
                store=store,
                encryptor=TokenEncryptor(),
                lock_session=session,
                fetch_fn=upstream.fetch_credits,
                consume_fn=delayed_consume,
                refresh_usage=scheduler._refresh_usage_after_auto_redeem,
            )

    monkeypatch.setattr(scheduler, "_auto_redeem_reset_credit", started_auto)
    manual = asyncio.create_task(manual_reset())
    automatic = None
    try:
        await asyncio.wait_for(consuming.wait(), timeout=2)
        automatic = asyncio.create_task(_tick(account, upstream))
        await asyncio.wait_for(automatic_started.wait(), timeout=2)
        release.set()
        await asyncio.wait_for(asyncio.gather(manual, automatic), timeout=5)
    finally:
        release.set()
        for task in (manual, automatic):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (manual, automatic) if task is not None), return_exceptions=True)
    assert upstream.consumed == ["coupon-1"]


@pytest.mark.asyncio
async def test_ambiguous_early_consume_stays_pinned_after_day_and_at_expiry(async_client, monkeypatch):
    account, upstream = await _setup(monkeypatch)
    upstream.fail_consume = True
    await _tick(account, upstream)
    assert upstream.consumed == ["coupon-1"]
    upstream.advance(timedelta(days=2))
    upstream.reset_at = int((upstream.now + timedelta(days=5)).timestamp())
    await _write_candidate(account, upstream)
    await _tick(account, upstream)
    assert upstream.consumed == ["coupon-1"]
    upstream.now = upstream.expires_at - timedelta(minutes=4)
    await _tick(account, upstream)
    assert upstream.consumed == ["coupon-1"]


@pytest.mark.asyncio
async def test_multiday_subscription_replenishment_and_expiry_fallback(async_client, monkeypatch):
    account, upstream = await _setup(monkeypatch)
    await _tick(account, upstream)
    assert upstream.consumed == ["coupon-1"]
    upstream.serve(99)
    upstream.advance(timedelta(days=2))
    await _write_candidate(account, upstream)
    await _tick(account, upstream)
    assert upstream.consumed == ["coupon-1", "coupon-2"]

    upstream.serve(99)
    upstream.advance(timedelta(days=5))
    await _write_candidate(account, upstream)
    await _tick(account, upstream)
    assert upstream.consumed == ["coupon-1", "coupon-2"]  # natural refresh is only two days away
    assert upstream.included_served == 198
    assert upstream.paid_served == 0
    upstream.advance(timedelta(days=2))
    await _write_candidate(account, upstream)
    await _tick(account, upstream)
    assert upstream.natural_refills == 1
    assert upstream.used == 0
    assert upstream.consumed == ["coupon-1", "coupon-2"]
    upstream.advance(upstream.expires_at - timedelta(minutes=4) - upstream.now)
    upstream.serve(25)
    await _tick(account, upstream)
    assert upstream.consumed == ["coupon-1", "coupon-2", "coupon-3"]
    upstream.serve(100)
    assert upstream.included_served == 323
    assert upstream.paid_served == 0


@pytest.mark.asyncio
async def test_early_redemption_extends_existing_expiry_fallback_guard(async_client, monkeypatch):
    account, upstream = await _setup(monkeypatch)
    upstream.expires_at = upstream.now + timedelta(minutes=10)
    await _tick(account, upstream)
    assert upstream.consumed == ["coupon-1"]
    # Simulate an earlier guard almost at its deadline, followed by useful
    # renewed depletion while the coupon is still outside the rescue window.
    async with SessionLocal() as session:
        rows = (
            await session.scalars(
                select(ResetCreditRedeemRequest).where(ResetCreditRedeemRequest.account_id == account.id)
            )
        ).all()
        guard = next(row for row in rows if row.redeem_request_id.startswith("auto-reset-credit:"))
        guard.expires_at = upstream.now + timedelta(minutes=1)
        await session.commit()
    upstream.serve(100)
    await _write_candidate(account, upstream)
    await _tick(account, upstream)
    assert upstream.consumed == ["coupon-1", "coupon-2"]

    upstream.advance(timedelta(minutes=6))
    await _tick(account, upstream)
    assert upstream.consumed == ["coupon-1", "coupon-2"]


@pytest.mark.asyncio
async def test_expiry_crossing_cannot_retarget_rescue_to_later_coupon(async_client, monkeypatch):
    account, upstream = await _setup(monkeypatch)
    upstream.used = 0.0
    soon = upstream.now + timedelta(seconds=1)
    later = upstream.now + timedelta(days=17)

    async def credits_with_different_expiries(*args, **kwargs):
        credits = [
            {"id": credit_id, "status": "available", "expires_at": expiry}
            for credit_id, expiry in (("coupon-1", soon), ("coupon-2", later))
            if expiry >= upstream.now
        ]
        return ResetCreditsResponse.model_validate({"available_count": len(credits), "credits": credits})

    original_auto = scheduler._auto_redeem_reset_credit

    async def expire_first_candidate(*args, **kwargs):
        upstream.advance(timedelta(seconds=2))
        return await original_auto(*args, **kwargs)

    upstream.fetch_credits = credits_with_different_expiries
    monkeypatch.setattr(scheduler, "_auto_redeem_reset_credit", expire_first_candidate)
    await _tick(account, upstream)
    assert upstream.consumed == []
