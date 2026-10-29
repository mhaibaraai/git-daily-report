"""修正已填日报的累计进展。

每个工单 ID 从自身第一天到结束日，按排期日独立线性分配到 100%。
只改百分比，工时与文字保持 EOM 原样，由提交流程按日报 ID 覆盖。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from .models import ScheduleDay
from .sources.eom import EomClient, _order
from .validate import number, validate_days


def propose(client: EomClient, since: date, until: date, today: date | None = None) -> list[ScheduleDay]:
    """区间内已填日报的重排结果；与目标值不同的标为 amend，未填的留空。"""
    bounded, chains = client.continuity(since, until, today)
    days = []
    for day, raws in sorted(bounded.items()):
        orders = []
        for raw in raws:
            order = _order(raw)
            chain = chains.get(order.id, [])
            if chain and chain[0] < since.isoformat():
                raise ValueError(f"工单 {order.id}「{order.title}」的排期从 {chain[0]} 开始，请把起始日期提前到 {chain[0]}")
            p = order.progress
            if p is not None and p.report_id and day in chain:
                target = _linear(chain.index(day) + 1, len(chain))
                if number(p.rate) != target:
                    order = replace(order, progress=replace(p, source="amend", rate=target, original_rate=p.rate))
            orders.append(order)
        days.append(ScheduleDay(day, tuple(orders)))
    return validate_days(days)


def _linear(position: int, total: int) -> int:
    """第 position 个排期日的累计进展，四舍五入到整数。"""
    return (200 * position + total) // (2 * total)
