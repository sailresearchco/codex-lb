from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.utils.time import naive_utc_to_epoch
from app.db.models import ApiKey, RequestLog
from app.db.session import SessionLocal
from app.modules.accounts.repository import AccountsRepository
from app.modules.dashboard.repository import DashboardRepository
from app.modules.usage.repository import UsageRepository
from tests.integration.test_dashboard_overview import _make_account

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 9, 18, 12)
SINCE = NOW - timedelta(hours=2)


async def _seed_attribution() -> None:
    async with SessionLocal() as session:
        await AccountsRepository(session).upsert(_make_account("attribution-account", "owner@example.com", "pro"))
        await UsageRepository(session).add_entry(
            "attribution-account",
            50.0,
            window="secondary",
            window_minutes=10_080,
            reset_at=int(naive_utc_to_epoch(NOW + timedelta(days=1))),
            recorded_at=NOW - timedelta(minutes=1),
        )
        session.add_all(
            [
                ApiKey(id="frequent", name="Frequent", key_hash="frequent-hash", key_prefix="fre"),
                ApiKey(id="large", name="Large", key_hash="large-hash", key_prefix="lar"),
            ]
        )
        # Equal model counts and token totals resolve by model name, while
        # reasoning tokens contribute only when output tokens are absent.
        for index, model in enumerate(("model-z", "model-a", "model-z", "model-a")):
            session.add(
                RequestLog(
                    api_key_id="frequent",
                    request_id=f"frequent-{index}",
                    requested_at=NOW - timedelta(minutes=index + 1),
                    model=model,
                    status="success",
                    input_tokens=5,
                    output_tokens=5 if index < 2 else None,
                    reasoning_tokens=500 if index < 2 else 5,
                    cached_input_tokens=2,
                )
            )
        for index in range(3):
            session.add(
                RequestLog(
                    api_key_id=None,
                    request_id=f"unattributed-{index}",
                    requested_at=NOW - timedelta(minutes=10 + index),
                    model="model-null-key",
                    status="error",
                    input_tokens=1,
                )
            )
        # Both inclusive boundaries count; a removed key remains attributed
        # by its ID even though its name can no longer be joined.
        for index in range(2):
            session.add(
                RequestLog(
                    api_key_id="removed",
                    request_id=f"removed-{index}",
                    requested_at=SINCE + timedelta(microseconds=index),
                    model="model-removed-key",
                    status="success",
                    input_tokens=1,
                )
            )
        session.add(
            RequestLog(
                api_key_id="large",
                request_id="large-at-upper-bound",
                requested_at=NOW,
                model="model-large",
                status="success",
                input_tokens=1_000,
                output_tokens=0,
                reasoning_tokens=900,
                cached_input_tokens=100,
            )
        )
        for request_id, requested_at, deleted_at, kind in (
            ("before", SINCE - timedelta(microseconds=1), None, "normal"),
            ("after", NOW + timedelta(microseconds=1), None, "normal"),
            ("deleted", NOW, NOW, "normal"),
            ("warmup", NOW, None, "warmup"),
            ("limit-warmup", NOW, None, "limit_warmup"),
        ):
            session.add(
                RequestLog(
                    api_key_id="excluded",
                    request_id=request_id,
                    requested_at=requested_at,
                    deleted_at=deleted_at,
                    request_kind=kind,
                    model="excluded-model",
                    status="success",
                    input_tokens=100_000,
                )
            )
        await session.commit()


@pytest.mark.parametrize("endpoint", ["overview", "projections"])
async def test_dashboard_attribution_preserves_rankings_and_raw_window(
    async_client, db_setup, monkeypatch: pytest.MonkeyPatch, endpoint: str
):
    monkeypatch.setattr("app.modules.dashboard.service.utcnow", lambda: NOW)
    await _seed_attribution()

    response = await async_client.get(f"/api/dashboard/{endpoint}")

    assert response.status_code == 200
    rows = response.json()["weeklyCreditPace"]["topApiKeys"]
    assert [row["apiKeyId"] for row in rows] == ["frequent", None, "removed", "large"]
    assert [row["requests"] for row in rows] == [4, 3, 2, 1]
    assert [row["billableTokens"] for row in rows] == [40, 3, 2, 1_000]
    assert [row["cachedTokens"] for row in rows] == [8, 0, 0, 100]
    assert rows[0]["dominantModel"] == "model-a"


async def test_attribution_unions_both_top_limits_without_duplicate_keys(db_setup):
    await _seed_attribution()

    async with SessionLocal() as session:
        rows = await DashboardRepository(session).top_api_key_attribution_since(SINCE, now=NOW, per_metric_limit=1)

    assert [row.api_key_id for row in rows] == ["frequent", "large"]
    assert [row.requests for row in rows] == [4, 1]
    assert [row.billable_tokens for row in rows] == [40, 1_000]
