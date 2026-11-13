from datetime import UTC, datetime

import pytest

from app.core.usage.reset_credits_refresh_scheduler import _early_weekly_reset_qualifies

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("used", "seconds_to_reset", "minutes", "expected"),
    [
        (99.0, 72 * 3600, 10080, True),
        (100.0, 7 * 86400, 10080, True),
        (98.999, 7 * 86400, 10080, False),
        (99.0, 72 * 3600 - 1, 10080, False),
        (100.0, None, 10080, False),
        (100.0, 7 * 86400, None, False),
        (100.0, 7 * 86400, 300, False),
        (100.0, 7 * 86400, 43200, False),
        (float("nan"), 7 * 86400, 10080, False),
        (float("inf"), 7 * 86400, 10080, False),
    ],
)
def test_conservative_weekly_thresholds(used, seconds_to_reset, minutes, expected):
    now = datetime(2026, 11, 12, tzinfo=UTC)
    reset_at = int(now.timestamp()) + seconds_to_reset if seconds_to_reset is not None else None
    assert (
        _early_weekly_reset_qualifies(used_percent=used, reset_at=reset_at, window_minutes=minutes, now=now) is expected
    )
