"""工时提醒。

EOM 页面有条硬规则：当天所有工单的实际工时合计不得超过 7.5 小时，超了整批
拒收。工具不再提交 EOM，但分配工时时提前把这条摆出来，省得粘贴回去才被打回。
"""

from __future__ import annotations

from typing import Iterable

from .models import WorkPoint

DAILY_HOURS_CAP = 7.5


def day_hours(points: Iterable[WorkPoint]) -> float:
    """当天工时合计，未填按 0 计。"""
    return sum(float(point.hours or 0) for point in points)


def hours_warning(points: Iterable[WorkPoint]) -> str | None:
    """超过上限时给出提醒，未超返回 None。"""
    total = day_hours(points)
    if total <= DAILY_HOURS_CAP:
        return None
    return f"当日工时合计 {_trim(total)} 小时，超过 EOM 上限 {_trim(DAILY_HOURS_CAP)} 小时"


def _trim(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
