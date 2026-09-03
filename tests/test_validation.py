"""当日工时上限提醒。"""

from __future__ import annotations

from git_daily_report.models import WorkPoint
from git_daily_report.validation import DAILY_HOURS_CAP, day_hours, hours_warning


def point(hours: float | None) -> WorkPoint:
    return WorkPoint(
        id="2026-08-10/A", day="2026-08-10", title="任务", entries=(), confidence="exact", hours=hours
    )


def test_missing_hours_count_as_zero():
    assert day_hours([point(None), point(2.5)]) == 2.5


def test_at_the_cap_is_allowed():
    assert hours_warning([point(DAILY_HOURS_CAP)]) is None


def test_over_the_cap_warns_with_both_numbers():
    warning = hours_warning([point(4), point(4)])
    assert warning is not None
    assert "8 小时" in warning
    assert "7.5 小时" in warning


def test_integer_totals_drop_the_decimal_point():
    assert "8 小时" in (hours_warning([point(8)]) or "")
