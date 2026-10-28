"""时间区间：一周写一次，默认本周。"""

from __future__ import annotations

from datetime import date, timedelta


def week_range(today: date | None = None) -> tuple[date, date]:
    """today 所在周的周一至周日。"""
    now = today or date.today()
    start = now - timedelta(days=now.weekday())
    return start, start + timedelta(days=6)


def resolve(
    *, since: str | None = None, until: str | None = None, today: date | None = None
) -> tuple[date, date]:
    """显式区间优先，否则取本周。"""
    if not (since or until):
        return week_range(today)
    low = _parse(since) if since else _parse(until)
    high = _parse(until) if until else low
    if high < low:
        raise ValueError(f"起始日期晚于结束日期：{low} ~ {high}")
    return low, high


def shortcut(name: str, today: date | None = None) -> tuple[date, date]:
    """页面上的快捷区间。"""
    now = today or date.today()
    if name == "week":
        return week_range(now)
    if name == "last-week":
        return week_range(now - timedelta(days=7))
    raise ValueError(f"未知的快捷区间：{name}")


def _parse(value: str | None) -> date:
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"日期格式应为 YYYY-MM-DD，收到：{value}") from exc
