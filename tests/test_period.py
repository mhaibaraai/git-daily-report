"""时间区间解析。"""

from __future__ import annotations

from datetime import date

import pytest

from git_daily_report.period import month_range, resolve, shortcut

TODAY = date(2026, 8, 12)  # 周三


def test_month_range_handles_month_lengths():
    assert month_range("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_range("2026-08") == (date(2026, 8, 1), date(2026, 8, 31))


def test_month_range_rejects_bad_format():
    with pytest.raises(ValueError, match="YYYY-MM"):
        month_range("2026/08")


def test_explicit_range_wins_over_month():
    assert resolve(since="2026-08-04", until="2026-08-10", month="2026-09") == (
        date(2026, 8, 4),
        date(2026, 8, 10),
    )


def test_single_bound_collapses_to_one_day():
    assert resolve(since="2026-08-04") == (date(2026, 8, 4), date(2026, 8, 4))


def test_reversed_range_is_rejected():
    with pytest.raises(ValueError, match="晚于"):
        resolve(since="2026-08-10", until="2026-08-04")


def test_today_keyword_and_bare_day():
    assert resolve(day="today", today=TODAY) == (TODAY, TODAY)
    assert resolve(day="2026-08-04") == (date(2026, 8, 4), date(2026, 8, 4))


def test_defaults_to_the_current_month():
    assert resolve(today=TODAY) == (date(2026, 8, 1), date(2026, 8, 31))


@pytest.mark.parametrize(
    "name,expected",
    [
        ("today", (date(2026, 8, 12), date(2026, 8, 12))),
        ("week", (date(2026, 8, 10), date(2026, 8, 12))),
        ("month", (date(2026, 8, 1), date(2026, 8, 31))),
        ("last-month", (date(2026, 7, 1), date(2026, 7, 31))),
    ],
)
def test_shortcuts(name, expected):
    assert shortcut(name, today=TODAY) == expected


def test_unknown_shortcut_is_rejected():
    with pytest.raises(ValueError, match="未知的快捷区间"):
        shortcut("yesterday", today=TODAY)
