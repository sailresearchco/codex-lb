from __future__ import annotations

import asyncio
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timedelta
from statistics import median
from typing import TypedDict, cast

import pytest
from aiosqlite import Connection as AsyncSQLiteConnection
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.retention.job as retention_job
import app.modules.reports.speed_histogram as histogram_module
from app.db.models import Account, AccountUsageRollupState, RequestLog, RequestReportSpeedHistogram
from app.db.session import SessionLocal, get_background_session
from app.modules.accounts.repository import AccountsRepository
from app.modules.accounts.usage_time_rollup import epoch_seconds
from app.modules.reports.filters import MISSING_USERAGENT_GROUP
from app.modules.reports.speed_histogram import (
    RELATIVE_ERROR_BOUND,
    TIMING_EDGES,
    TPS_EDGES,
    fold_next_speed_histogram_slice,
    histogram_median,
    run_speed_histogram_fold_pass,
)
from app.modules.reports.speed_histogram_read import read_speed_histograms
from app.modules.request_logs.repository import RequestLogsRepository
from tests.integration.test_request_usage_time_rollup import _make_account

pytestmark = pytest.mark.integration
_START = datetime(2026, 11, 1)


class _SpeedFilters(TypedDict, total=False):
    account_ids: list[str]
    api_key_ids: list[str]
    model: str
    useragent_group: str


@pytest.mark.parametrize(
    "metric,values",
    [
        ("ttft", [0, 0, 10, 21, 28, 40]),
        ("queue", [-100, -10, -1, 0, 1, 10]),
        ("tps", [0.0001, 10.5, 11.0, 91.0]),
        ("ttft", [-3, 3]),
        ("queue", [0, 0]),
    ],
)
def test_histogram_median_encloses_exact_odd_and_even_samples(metric, values):
    edges = TPS_EDGES if metric == "tps" else TIMING_EDGES
    assert len(edges) + 1 <= 507
    result = histogram_median(metric, Counter(bisect_right(edges, value) for value in values))
    exact = median(values)
    assert result.sample_count == len(values)
    assert result.lower_bound <= exact <= result.upper_bound
    assert not result.clipped
    if result.relative_error_bound is not None:
        assert abs(result.value - exact) <= RELATIVE_ERROR_BOUND * abs(exact) + 1e-12


@pytest.mark.parametrize("metric,value", [("ttft", 2**40), ("queue", -(2**40)), ("tps", 2**-40)])
def test_histogram_overflow_keeps_samples_without_finite_error_claim(metric, value):
    edges = TPS_EDGES if metric == "tps" else TIMING_EDGES
    result = histogram_median(metric, {bisect_right(edges, value): 3})
    assert result.sample_count == 3
    assert result.clipped
    assert result.relative_error_bound is None
    assert result.lower_bound is None or result.upper_bound is None


async def _seed_speed_rows():
    rows = [
        {
            "request_id": f"speed-{index}",
            "requested_at": _START + timedelta(hours=index),
            "account_id": "speed-a" if index % 2 else "speed-b",
            "api_key_id": f"key-{index % 3}",
            "model": f"model-{index % 3}",
            "useragent_group": None if index % 2 else "agent",
            "status": ("success", "error", "cancelled")[index % 3],
            "source": "limit_warmup" if index == 7 else None,
            "request_kind": "warmup" if index == 9 else "normal",
            "input_tokens": 100,
            "output_tokens": 100 + index,
            "reasoning_tokens": 10,
            "latency_ms": 1000 + index * 100,
            "latency_first_token_ms": None if index == 5 else 100 + index * 10,
            "latency_queue_ms": None if index % 4 == 0 else index * 5,
            "deleted_at": _START if index % 6 == 0 else None,
        }
        for index in range(26)
    ]
    async with SessionLocal() as session:
        for name in ("a", "b"):
            await AccountsRepository(session).upsert(_make_account(f"speed-{name}", f"speed-{name}@example.invalid"))
        await session.execute(insert(RequestLog), rows)
        await session.commit()
    return rows


@pytest.mark.asyncio
async def test_speed_histograms_preserve_filters_partial_hours_and_live_tail(db_setup):
    rows = await _seed_speed_rows()
    # Fold through21:00, leaving3.5 hours of live tail for this timezone day.
    for _ in range(4):
        await run_speed_histogram_fold_pass(now=_START + timedelta(hours=23))
    start, end = _START + timedelta(minutes=30), _START + timedelta(days=1, minutes=30)
    windows = [("2026-11-01", start, end)]
    filters: tuple[_SpeedFilters, ...] = (
        {},
        {"account_ids": ["speed-a"]},
        {"api_key_ids": ["key-1"]},
        {"model": "model-2"},
        {"useragent_group": "agent"},
        {"useragent_group": MISSING_USERAGENT_GROUP},
    )
    for selected in filters:
        async with SessionLocal() as session:
            result = await read_speed_histograms(session, windows, **selected)
        assert result.available
        assert result.folded_through == _START + timedelta(hours=21)
        eligible = [
            row
            for row in rows
            if start <= row["requested_at"] < end
            and row["source"] != "limit_warmup"
            and row["request_kind"] != "warmup"
        ]
        if "account_ids" in selected:
            eligible = [row for row in eligible if row["account_id"] in selected["account_ids"]]
        if "api_key_ids" in selected:
            eligible = [row for row in eligible if row["api_key_id"] in selected["api_key_ids"]]
        if "model" in selected:
            eligible = [row for row in eligible if row["model"] == selected["model"]]
        if "useragent_group" in selected:
            group = None if selected["useragent_group"] == MISSING_USERAGENT_GROUP else selected["useragent_group"]
            eligible = [row for row in eligible if row["useragent_group"] == group]
        samples = (
            [row["latency_first_token_ms"] for row in eligible if row["latency_first_token_ms"] is not None],
            [
                (row["output_tokens"] - row["reasoning_tokens"])
                * 1000
                / (row["latency_ms"] - row["latency_first_token_ms"])
                for row in eligible
                if row["latency_first_token_ms"] is not None
            ],
            [row["latency_queue_ms"] for row in eligible if row["latency_queue_ms"] is not None],
        )
        for estimate, values in zip(
            (result.daily[0].ttft, result.daily[0].tps, result.daily[0].queue), samples, strict=True
        ):
            assert estimate.sample_count == len(values)
            assert estimate.lower_bound <= median(values) <= estimate.upper_bound


@pytest.mark.asyncio
async def test_speed_histogram_cancel_rolls_back_counts_and_watermark(db_setup, monkeypatch):
    await _seed_speed_rows()
    original = histogram_module.histogram_fold_insert

    def interrupted(session, metric, start, end):
        if metric == "queue":
            raise asyncio.CancelledError()
        return original(session, metric, start, end)

    monkeypatch.setattr(histogram_module, "histogram_fold_insert", interrupted)
    with pytest.raises(asyncio.CancelledError):
        async with get_background_session() as session:
            await fold_next_speed_histogram_slice(session, _START + timedelta(hours=1))
    async with SessionLocal() as session:
        assert not (await session.scalars(select(RequestReportSpeedHistogram))).all()
        assert await session.scalar(select(AccountUsageRollupState.speed_folded_through)) == datetime(1970, 1, 1)
    monkeypatch.setattr(histogram_module, "histogram_fold_insert", original)
    async with get_background_session() as session:
        assert await fold_next_speed_histogram_slice(session, _START + timedelta(hours=1))
    async with get_background_session() as session:
        assert not await fold_next_speed_histogram_slice(session, _START + timedelta(hours=1))


@pytest.mark.asyncio
async def test_histogram_coverage_refuses_cold_tail_and_pruned_partial_hour(db_setup):
    await _seed_speed_rows()
    day = [("day", _START, _START + timedelta(days=1))]
    async with SessionLocal() as session:
        result = await read_speed_histograms(session, day)
        assert not result.available
        assert result.reason == "building"
    for _ in range(5):
        await run_speed_histogram_fold_pass(now=_START + timedelta(days=2))
    async with SessionLocal() as session:
        before = await read_speed_histograms(session, day)
        await session.execute(delete(RequestLog).where(RequestLog.requested_at < _START + timedelta(hours=22)))
        await session.commit()
        after = await read_speed_histograms(session, day)
        assert after == before
        partial = await read_speed_histograms(
            session, [("day", _START + timedelta(minutes=30), _START + timedelta(days=1))]
        )
        assert not partial.available
        assert partial.reason == "raw_history_pruned"


@pytest.mark.asyncio
async def test_speed_histogram_reads_have_sqlite_work_budget_cold_and_warm(db_setup):
    async with SessionLocal() as session:
        if session.get_bind().dialect.name != "sqlite":
            pytest.skip("SQLite virtual-machine work regression")
        await session.execute(
            insert(RequestLog),
            [
                {
                    "request_id": f"histogram-work-{index}",
                    "requested_at": _START + timedelta(seconds=index % 3600),
                    "model": "model",
                    "status": "success",
                    "latency_first_token_ms": index + 1,
                    "latency_queue_ms": index % 500,
                    "latency_ms": index + 1001,
                    "output_tokens": 100,
                    "reasoning_tokens": 10,
                }
                for index in range(10_000)
            ],
        )
        await session.commit()
    windows = [("day", _START, _START + timedelta(hours=1))]

    async def bounded_read():
        async with SessionLocal() as session:
            connection = await session.connection()
            driver = cast(AsyncSQLiteConnection, (await connection.get_raw_connection()).driver_connection)
            operations = 0

            def progress():
                nonlocal operations
                operations += 100
                return int(operations > 50_000)

            await driver.set_progress_handler(progress, 100)
            try:
                result = await read_speed_histograms(session, windows)
            finally:
                await driver.set_progress_handler(lambda: 0, 0)
            completed = operations
            assert await session.scalar(select(func.sum(RequestLog.output_tokens))) == 1_000_000
            assert operations == completed
            return result

    cold = await bounded_read()
    assert not cold.available
    assert cold.reason == "building"
    await run_speed_histogram_fold_pass(now=_START + timedelta(hours=3))
    warm = await bounded_read()
    assert warm.available
    for estimate, exact in zip(
        (warm.daily[0].ttft, warm.daily[0].tps, warm.daily[0].queue), (5000.5, 90.0, 249.5), strict=True
    ):
        assert estimate.sample_count == 10_000
        assert estimate.lower_bound <= exact <= estimate.upper_bound


@pytest.mark.asyncio
async def test_speed_histograms_mirror_soft_and_hard_account_deletion(db_setup):
    await _seed_speed_rows()
    for _ in range(5):
        await run_speed_histogram_fold_pass(now=_START + timedelta(days=2))
    windows = [("day", _START, _START + timedelta(days=1))]
    async with SessionLocal() as session:
        before = await read_speed_histograms(session, windows)
        account_b = await read_speed_histograms(session, windows, account_ids=["speed-b"])
        assert await AccountsRepository(session).delete("speed-a")
        after_soft = await read_speed_histograms(session, windows)
        assert after_soft == before
        removed = await read_speed_histograms(session, windows, account_ids=["speed-a"])
        assert removed.daily[0].ttft.sample_count == 0
        assert await AccountsRepository(session).delete("speed-b", delete_history=True)
        after_hard = await read_speed_histograms(session, windows)
        assert (
            after_hard.daily[0].ttft.sample_count
            == before.daily[0].ttft.sample_count - account_b.daily[0].ttft.sample_count
        )


@pytest.mark.asyncio
async def test_speed_histograms_merge_account_dimensions_without_losing_samples(db_setup):
    rows = await _seed_speed_rows()
    # A second account contributes to an already-populated histogram bin,
    # exercising merge-add rather than merely moving disjoint buckets.
    duplicate = {**rows[1], "request_id": "histogram-merge-collision", "account_id": "speed-b"}
    async with SessionLocal() as session:
        await session.execute(insert(RequestLog), [duplicate])
        await session.execute(
            update(Account).values(chatgpt_account_id="histogram-shared-identity", email="speed-a@example.invalid")
        )
        await session.commit()
    for _ in range(5):
        await run_speed_histogram_fold_pass(now=_START + timedelta(days=2))
    windows = [("day", _START, _START + timedelta(days=1))]
    async with SessionLocal() as session:
        before = await read_speed_histograms(session, windows)
        saved = await AccountsRepository(session).upsert(
            _make_account("speed-a", "speed-a@example.invalid", chatgpt_account_id="histogram-shared-identity"),
            merge_by_email=False,
            merge_by_chatgpt_identity=True,
        )
        assert saved.id == "speed-a"
        assert await read_speed_histograms(session, windows) == before
        merged = await read_speed_histograms(session, windows, account_ids=["speed-a"])
        assert merged.daily == before.daily
        removed = await read_speed_histograms(session, windows, account_ids=["speed-b"])
        assert removed.daily[0].ttft.sample_count == 0


@pytest.mark.asyncio
async def test_model_correction_does_not_rewrite_histogram_folded_rows(db_setup):
    await _seed_speed_rows()
    await run_speed_histogram_fold_pass(now=_START + timedelta(hours=3))
    async with SessionLocal() as session:
        assert await RequestLogsRepository(session).update_model_for_request("speed-0", "replacement-model") == 0
        assert await session.scalar(select(RequestLog.model).where(RequestLog.request_id == "speed-0")) == "model-0"


@pytest.mark.asyncio
async def test_current_day_and_future_day_do_not_require_future_fold_coverage(db_setup):
    await _seed_speed_rows()
    now = _START + timedelta(hours=12, minutes=30)
    async with SessionLocal() as session:
        await session.execute(delete(RequestLog).where(RequestLog.requested_at >= now))
        await session.commit()
    for _ in range(3):
        await run_speed_histogram_fold_pass(now=now)
    async with SessionLocal() as session:
        current = await read_speed_histograms(session, [("today", _START, _START + timedelta(days=1))], now=now)
        assert current.available
        assert current.folded_through == _START + timedelta(hours=10)
        assert current.daily[0].ttft.sample_count == 10
        future = await read_speed_histograms(
            session, [("future", _START + timedelta(days=2), _START + timedelta(days=3))], now=now
        )
        assert future.available
        assert future.daily[0].ttft.sample_count == 0


@pytest.mark.asyncio
async def test_histogram_first_partial_day_is_unavailable_before_proven_coverage(db_setup):
    await _seed_speed_rows()
    async with SessionLocal() as session:
        await session.execute(delete(RequestLog).where(RequestLog.requested_at == _START))
        await session.commit()
    for _ in range(5):
        await run_speed_histogram_fold_pass(now=_START + timedelta(days=2))
    async with SessionLocal() as session:
        result = await read_speed_histograms(session, [("day", _START, _START + timedelta(days=1))])
        assert not result.available
        assert result.reason == "raw_history_pruned"
        assert result.coverage_from == _START + timedelta(hours=1)
        covered = [("covered", _START + timedelta(hours=1), _START + timedelta(days=1))]
        before = await read_speed_histograms(session, covered)
        assert before.available
        await session.execute(delete(RequestLog).where(RequestLog.requested_at < _START + timedelta(hours=12)))
        await session.commit()
        assert await read_speed_histograms(session, covered) == before


@pytest.mark.asyncio
async def test_backdated_append_updates_folded_histogram_without_claiming_unknown_history(db_setup):
    await _seed_speed_rows()
    for _ in range(5):
        await run_speed_histogram_fold_pass(now=_START + timedelta(days=2))
    window = [("day", _START, _START + timedelta(days=1))]
    async with SessionLocal() as session:
        before = await read_speed_histograms(session, window)
        repo = RequestLogsRepository(session)
        for request_id, at in (
            ("late-covered", _START + timedelta(hours=6)),
            ("late-uncovered", _START - timedelta(hours=3)),
        ):
            await repo.add_log(
                account_id="speed-a",
                request_id=request_id,
                model="late-model",
                input_tokens=30,
                output_tokens=110,
                reasoning_tokens=10,
                latency_ms=1013,
                latency_first_token_ms=13,
                latency_queue_ms=7,
                status="error",
                error_code="upstream_500",
                requested_at=at,
                api_key_id="late-key",
                useragent_group="late-agent",
            )
        after = await read_speed_histograms(session, window)
        assert after.daily[0].ttft.sample_count == before.daily[0].ttft.sample_count + 1
        filtered = await read_speed_histograms(
            session,
            window,
            account_ids=["speed-a"],
            api_key_ids=["late-key"],
            model="late-model",
            useragent_group="late-agent",
        )
        for estimate, exact in zip(
            (filtered.daily[0].ttft, filtered.daily[0].tps, filtered.daily[0].queue), (13.0, 100.0, 7.0), strict=True
        ):
            assert estimate.sample_count == 1
            assert estimate.lower_bound is not None and estimate.upper_bound is not None
            assert estimate.lower_bound <= exact <= estimate.upper_bound
        assert after.coverage_from == _START
        uncovered = await read_speed_histograms(session, [("unknown", _START - timedelta(days=1), _START)])
        assert not uncovered.available
        assert uncovered.reason == "raw_history_pruned"
        assert (
            await session.scalar(
                select(func.sum(RequestReportSpeedHistogram.sample_count)).where(
                    RequestReportSpeedHistogram.bucket_epoch < epoch_seconds(_START),
                    RequestReportSpeedHistogram.metric == "ttft",
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_backdated_append_failure_rolls_back_raw_row_and_histogram(db_setup, monkeypatch):
    await _seed_speed_rows()
    for _ in range(5):
        await run_speed_histogram_fold_pass(now=_START + timedelta(days=2))
    original = histogram_module.fold_late_speed_log

    async def interrupted(session, log):
        await original(session, log)
        raise asyncio.CancelledError()

    monkeypatch.setattr(histogram_module, "fold_late_speed_log", interrupted)
    window = [("day", _START, _START + timedelta(days=1))]
    async with SessionLocal() as session:
        before = await read_speed_histograms(session, window)
        with pytest.raises(asyncio.CancelledError):
            await RequestLogsRepository(session).add_log(
                account_id="speed-a",
                request_id="late-cancelled",
                model="model",
                input_tokens=10,
                output_tokens=10,
                latency_ms=100,
                latency_first_token_ms=10,
                status="success",
                error_code=None,
                requested_at=_START + timedelta(hours=6),
            )
        assert await session.scalar(select(func.count()).where(RequestLog.request_id == "late-cancelled")) == 0
        assert await read_speed_histograms(session, window) == before


async def _prepare_speed_retention() -> datetime:
    await _seed_speed_rows()
    async with SessionLocal() as session:
        session.add(
            RequestLog(
                request_id="pruned-boundary",
                requested_at=_START + timedelta(hours=18, minutes=30),
                model="model-0",
                status="success",
                latency_first_token_ms=17,
            )
        )
        await session.commit()
    now = _START + timedelta(days=2)
    for _ in range(5):
        await run_speed_histogram_fold_pass(now=now)
    async with SessionLocal() as session:
        # The other projections are irrelevant to speed samples, but must
        # certify their own progress before the real retention path prunes.
        watermark = now - timedelta(hours=2)
        await session.execute(
            update(AccountUsageRollupState).values(
                folded_through=watermark,
                hourly_folded_through=watermark,
                conversation_folded_through=watermark,
                reports_folded_through=watermark,
            )
        )
        await session.commit()
    return now


@pytest.mark.asyncio
async def test_retention_frontier_survives_late_append_and_preserves_speed_routes(async_client, db_setup):
    now = await _prepare_speed_retention()
    boundary = _START + timedelta(hours=18, minutes=30)
    cutoff = boundary + timedelta(minutes=1)
    whole = [("hour", _START + timedelta(hours=18), _START + timedelta(hours=19))]
    partial = [("partial", boundary, _START + timedelta(hours=19))]
    async with SessionLocal() as session:
        before = await read_speed_histograms(session, whole)
        assert before.available
        assert before.daily[0].ttft.sample_count == 2
    assert await retention_job._prune_request_logs(cutoff, now=now) == 20
    # Asia/Kolkata's midnight is exactly the inclusive deleted frontier.
    params = {"start_date": "2026-11-02", "end_date": "2026-11-02", "timezone": "Asia/Kolkata"}
    first = await async_client.get("/api/reports/speed", params=params)
    assert first.status_code == 200
    assert first.json()["unavailableReason"] == "raw_history_pruned"
    async with SessionLocal() as session:
        assert await read_speed_histograms(session, whole) == before
        assert await session.scalar(select(AccountUsageRollupState.request_logs_pruned_through)) == boundary
        await RequestLogsRepository(session).add_log(
            account_id="speed-a",
            request_id="late-after-retention",
            requested_at=boundary - timedelta(minutes=15),
            model="late-model",
            status="success",
            error_code=None,
            input_tokens=10,
            output_tokens=10,
            latency_ms=100,
            latency_first_token_ms=10,
        )
        earliest_raw = await session.scalar(select(func.min(RequestLog.requested_at)))
        assert earliest_raw is not None and earliest_raw < boundary
        unavailable = await read_speed_histograms(session, partial)
        assert not unavailable.available
        assert unavailable.reason == "raw_history_pruned"
        assert unavailable.coverage_from == _START
        after = await read_speed_histograms(session, whole)
        assert after.available
        assert after.daily[0].ttft.sample_count == before.daily[0].ttft.sample_count + 1
    after_late = await async_client.get("/api/reports/speed", params=params)
    assert after_late.status_code == 200
    assert after_late.json()["available"] is False
    assert after_late.json()["unavailableReason"] == "raw_history_pruned"
    empty = await async_client.get("/api/reports/speed", params={**params, "model": "missing-model"})
    assert empty.status_code == 200
    assert empty.json()["available"] is True
    assert empty.json()["daily"][0]["ttft"]["sampleCount"] == 0
    # Pruning an older late insert cannot rewind the previous frontier.
    assert await retention_job._prune_request_logs(cutoff, now=now) == 1
    assert await retention_job._prune_request_logs(cutoff, now=now) == 0
    async with SessionLocal() as session:
        assert await session.scalar(select(AccountUsageRollupState.request_logs_pruned_through)) == boundary
        assert await read_speed_histograms(session, whole) == after


@pytest.mark.asyncio
async def test_retention_frontier_commits_only_completed_batches(db_setup, monkeypatch):
    now = await _prepare_speed_retention()
    monkeypatch.setattr(retention_job, "BATCH_SIZE", 2)
    original = AsyncSession.commit
    batches = 0

    async def interrupted(session):
        nonlocal batches
        batches += 1
        if batches == 2:
            assert await session.scalar(select(AccountUsageRollupState.request_logs_pruned_through)) == (
                _START + timedelta(hours=3)
            )
            raise asyncio.CancelledError()
        await original(session)

    monkeypatch.setattr(AsyncSession, "commit", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await retention_job._prune_request_logs(_START + timedelta(hours=19), now=now)
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(RequestLog)) == 25
        assert await session.scalar(select(AccountUsageRollupState.request_logs_pruned_through)) == (
            _START + timedelta(hours=1)
        )
        assert await session.scalar(select(func.min(RequestLog.requested_at))) == _START + timedelta(hours=2)
