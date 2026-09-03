"""把命令行与页面上的各种时间写法解析成一对日期边界。"""

from __future__ import annotations

import calendar
from datetime import date, timedelta


def month_range(month: str) -> tuple[date, date]:
    """把 YYYY-MM 转成当月首末日。"""
    try:
        year_text, month_text = month.split("-")
        year, month_num = int(year_text), int(month_text)
        first = date(year, month_num, 1)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"月份格式应为 YYYY-MM，收到：{month}") from exc
    return first, date(year, month_num, calendar.monthrange(year, month_num)[1])


def resolve(
    *,
    since: str | None = None,
    until: str | None = None,
    month: str | None = None,
    day: str | None = None,
    today: date | None = None,
) -> tuple[date, date]:
    """优先级：显式区间 > 单日 > 月份 > 当月。"""
    now = today or date.today()

    if since or until:
        low = _parse(since) if since else _parse(until)
        high = _parse(until) if until else low
        if high < low:
            raise ValueError(f"起始日期晚于结束日期：{low} ~ {high}")
        return low, high

    if day:
        one = now if day == "today" else _parse(day)
        return one, one

    return month_range(month or f"{now.year:04d}-{now.month:02d}")


def shortcut(name: str, today: date | None = None) -> tuple[date, date]:
    """页面上的快捷区间。"""
    now = today or date.today()
    if name == "today":
        return now, now
    if name == "week":
        start = now - timedelta(days=now.weekday())
        return start, now
    if name == "month":
        return month_range(f"{now.year:04d}-{now.month:02d}")
    if name == "last-month":
        last_day_prev = now.replace(day=1) - timedelta(days=1)
        return month_range(f"{last_day_prev.year:04d}-{last_day_prev.month:02d}")
    raise ValueError(f"未知的快捷区间：{name}")


def _parse(value: str | None) -> date:
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"日期格式应为 YYYY-MM-DD，收到：{value}") from exc
