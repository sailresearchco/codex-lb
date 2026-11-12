from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, insert
from sqlalchemy.dialects import postgresql

from app.db import dashboard_reads
from app.db.models import RequestLog
from app.db.session import SessionLocal, engine
from app.modules.accounts.usage_time_rollup import run_hourly_fold_pass
from app.modules.request_logs.repository import RequestLogsRepository

pytestmark = pytest.mark.integration
NOW = datetime(2026, 11, 12, 16, 37)


async def test_earliest_activity_handles_empty_excluded_and_unknown_pre_epoch_history(db_setup, monkeypatch):
    async with SessionLocal() as session:
        repo = RequestLogsRepository(session)
        assert await repo.earliest_activity_at() is None
        await session.execute(
            insert(RequestLog),
            [
                dict(
                    request_id=str(i),
                    requested_at=datetime(1950, 1, 1),
                    model="model",
                    status="success",
                    request_kind=kind,
                )
                for i, kind in enumerate(("warmup", "limit_warmup"))
            ],
        )
        assert await repo.earliest_activity_at() is None
        session.add(RequestLog(request_id="normal", requested_at=NOW, model="model", status="success"))
        await session.flush()
        assert await repo.earliest_activity_at() == NOW
        at = datetime(1960, 1, 1, 12, 34, 56)
        session.add(
            RequestLog(
                request_id="unknown",
                requested_at=at,
                model="model",
                status="success",
                request_kind="future-kind",
                deleted_at=NOW,
            )
        )
        await session.flush()
        # Capture the actual statements, without mocking their results. The
        # PostgreSQL compile covers the correlated recursive query as well.
        statements = []
        execute = session.execute

        async def capture(statement, *args, **kwargs):
            statements.append(statement)
            return await execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", capture)
        assert await repo.earliest_activity_at() == at
        assert len(statements) == 2  # One raw snapshot, one unchanged rollup fallback.
        statements[0].compile(dialect=postgresql.dialect())


async def test_earliest_activity_preserves_subhour_and_retained_rollup_fallback(db_setup):
    first = NOW.replace(hour=10, minute=17)
    later = NOW.replace(hour=12, minute=42)
    async with SessionLocal() as session:
        session.add_all(
            [
                RequestLog(request_id="first", requested_at=first, model="model", status="success"),
                RequestLog(request_id="later", requested_at=later, model="model", status="success", deleted_at=NOW),
            ]
        )
        await session.commit()
    while await run_hourly_fold_pass(now=NOW):
        pass
    async with SessionLocal() as session:
        repo = RequestLogsRepository(session)
        assert await repo.earliest_activity_at() == first
        await session.execute(delete(RequestLog).where(RequestLog.request_id == "first"))
        assert await repo.earliest_activity_at() == first.replace(minute=0)
        await session.execute(delete(RequestLog))
        assert await repo.earliest_activity_at() == first.replace(minute=0)
        # A later backdated raw write must still win over the retained fold.
        older = first - timedelta(hours=1)
        session.add(
            RequestLog(
                request_id="backdated", requested_at=older, model="model", status="success", request_kind="unknown"
            )
        )
        await session.flush()
        assert await repo.earliest_activity_at() == older


@pytest.mark.parametrize("with_normal", [False, True])
async def test_overview_earliest_lookup_skips_twenty_thousand_excluded_rows(async_client, monkeypatch, with_normal):
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite VM work regression")
    monkeypatch.setattr("app.modules.dashboard.service.utcnow", lambda: NOW)
    async with SessionLocal() as session:
        await session.execute(
            insert(RequestLog),
            [
                dict(
                    request_id=str(i),
                    requested_at=NOW - timedelta(days=30),
                    model="model",
                    status="success",
                    request_kind="warmup" if i % 2 else "limit_warmup",
                )
                for i in range(20000)
            ],
        )
        if with_normal:
            session.add(
                RequestLog(
                    request_id="normal", requested_at=NOW - timedelta(minutes=1), model="model", status="success"
                )
            )
        await session.commit()
    operations = 0
    original = dashboard_reads._ReadBudget.progress

    def progress(budget):
        nonlocal operations
        operations += 1
        return int(operations > 25000 or original(budget))

    monkeypatch.setattr(dashboard_reads, "_PROGRESS_STEPS", 1)
    monkeypatch.setattr(dashboard_reads._ReadBudget, "progress", progress)
    response = await async_client.get("/api/dashboard/overview", params={"timeframe": "7d"})
    assert response.status_code == 200, response.json()
    summary = response.json()["summary"]
    assert summary["metrics"]["requests"] == int(with_normal)
    assert summary["comparison"]["canCompare"] is False
    assert 0 < operations <= 25000
