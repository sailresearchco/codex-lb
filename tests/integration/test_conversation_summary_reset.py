from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.time import utcnow
from app.db.models import (
    AccountUsageRollupState,
    RequestConversationFacet,
    RequestConversationModelSummary,
    RequestConversationSummary,
    RequestLog,
)
from app.db.session import SessionLocal
from app.modules.request_logs.conversation_summary import backfill_conversation_summaries
from app.modules.request_logs.conversation_summary_reset import reset_conversation_summaries, reset_next_batch

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_offline_reset_resumes_after_cancellation_and_rebuilds_old_writer_mutations(
    async_client, db_setup, monkeypatch
):
    now = utcnow()
    async with SessionLocal() as session:
        await session.execute(
            insert(RequestLog),
            [
                dict(
                    id=index + 1,
                    request_id=f"reset-{index}",
                    model="original",
                    conversation_id="thread",
                    status="success",
                    requested_at=now,
                    input_tokens=10,
                    output_tokens=2,
                    cost_usd=1.0,
                )
                for index in range(1003)
            ],
        )
        await session.commit()
    while await backfill_conversation_summaries():
        pass
    async with SessionLocal() as session:
        # Simulate an older writer that knows nothing about included projections.
        await session.execute(
            update(RequestLog).where(RequestLog.id == 1).values(model="updated", input_tokens=30, cost_usd=4.0)
        )
        await session.execute(delete(RequestLog).where(RequestLog.id == 2))
        await session.commit()
        raw_snapshot = list(
            (
                await session.execute(
                    select(RequestLog.id, RequestLog.model, RequestLog.input_tokens, RequestLog.cost_usd).order_by(
                        RequestLog.id
                    )
                )
            ).all()
        )
        assert await session.scalar(select(RequestConversationSummary.request_count)) == 1003

    assert not await reset_next_batch()  # Persist the marker before changing any rows.
    async with SessionLocal() as session:
        assert await session.scalar(select(AccountUsageRollupState.conversation_reset_after_id)) == 0
        assert (
            await session.scalar(select(func.count()).where(RequestLog.conversation_summary_included.is_(False))) == 0
        )
    for path in ("/api/conversations", "/api/conversations/thread"):
        response = await async_client.get(path)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "conversation_summaries_building"
    assert not await reset_next_batch()

    async def cancel_commit(self: AsyncSession) -> None:
        raise asyncio.CancelledError("interrupted reset page")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", cancel_commit)
        with pytest.raises(asyncio.CancelledError):
            await reset_next_batch()
    async with SessionLocal() as session:
        assert await session.scalar(select(AccountUsageRollupState.conversation_reset_after_id)) == 501
        assert (
            await session.scalar(select(func.count()).where(RequestLog.conversation_summary_included.is_(False))) == 500
        )
    assert await backfill_conversation_summaries() == 0
    async with SessionLocal() as session:
        assert await session.scalar(select(RequestConversationSummary.request_count)) == 1003

    # Finish the remaining raw pages, then interrupt the final invalidation too.
    assert not await reset_next_batch()
    assert not await reset_next_batch()
    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", cancel_commit)
        with pytest.raises(asyncio.CancelledError):
            await reset_next_batch()
    async with SessionLocal() as session:
        assert await session.scalar(select(AccountUsageRollupState.conversation_reset_after_id)) == 1003
        assert await session.scalar(select(RequestConversationSummary.request_count)) == 1003
    assert await backfill_conversation_summaries() == 0

    await reset_conversation_summaries()
    async with SessionLocal() as session:
        assert await session.scalar(select(AccountUsageRollupState.conversation_reset_after_id)) is None
        assert (
            list(
                (
                    await session.execute(
                        select(RequestLog.id, RequestLog.model, RequestLog.input_tokens, RequestLog.cost_usd).order_by(
                            RequestLog.id
                        )
                    )
                ).all()
            )
            == raw_snapshot
        )
        assert (
            await session.scalar(select(func.count()).where(RequestLog.conversation_summary_included.is_(False)))
            == 1002
        )
        for model in (RequestConversationSummary, RequestConversationFacet, RequestConversationModelSummary):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    assert (await async_client.get("/api/conversations")).status_code == 503
    while await backfill_conversation_summaries():
        pass
    response = await async_client.get("/api/conversations")
    assert response.status_code == 200
    row = response.json()["conversations"][0]
    assert row["requestCount"] == 1002
    assert row["totalTokens"] == 12044
    assert row["totalCostUsd"] == 1005.0
    detail = (await async_client.get("/api/conversations/thread")).json()
    assert {row["modelEffort"]["model"]: row["reqs"] for row in detail["modelStats"]} == {
        "original": 1001,
        "updated": 1,
    }
