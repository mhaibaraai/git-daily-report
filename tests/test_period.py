"""时间区间解析：按周为主。"""

from __future__ import annotations

from datetime import date

import pytest

from git_daily_report.period import resolve, shortcut, week_range

TODAY = date(2026, 10, 28)  # 周三


def test_week_range_is_monday_to_sunday():
    assert week_range(TODAY) == (date(2026, 10, 26), date(2026, 11, 1))


def test_defaults_to_this_week():
    assert resolve(today=TODAY) == (date(2026, 10, 26), date(2026, 11, 1))


def test_explicit_range_is_kept():
    assert resolve(since="2026-10-19", until="2026-10-23") == (date(2026, 10, 19), date(2026, 10, 23))


def test_single_bound_collapses_to_one_day():
    assert resolve(since="2026-10-19") == (date(2026, 10, 19), date(2026, 10, 19))


def test_reversed_range_is_rejected():
    with pytest.raises(ValueError, match="晚于"):
        resolve(since="2026-10-23", until="2026-10-19")


def test_bad_date_is_rejected():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        resolve(since="2026/10/19")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("week", (date(2026, 10, 26), date(2026, 11, 1))),
        ("last-week", (date(2026, 10, 19), date(2026, 10, 25))),
    ],
)
def test_shortcuts(name, expected):
    assert shortcut(name, today=TODAY) == expected


@pytest.mark.parametrize("name", ["today", "month", "last-month", "yesterday"])
def test_only_weekly_shortcuts_exist(name):
    with pytest.raises(ValueError, match="未知的快捷区间"):
        shortcut(name, today=TODAY)
