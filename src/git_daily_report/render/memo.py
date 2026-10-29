"""三段式文本。

段名与写法取自 EOM 日程表的「自动编排」解析规则：每段独占一行，「段名：内容」，
条目之间用全角分号。EOM 强制三段齐全，没有内容的段写「无」。
"""

from __future__ import annotations

from typing import Mapping, Sequence

from ..models import ScheduleDay

SECTION_KEYS = ("task", "data", "problem")

SECTION_LABELS: Mapping[str, str] = {
    "task": "具体任务",
    "data": "关键数据",
    "problem": "问题解决过程",
}

ENTRY_SEPARATOR = "；"
EMPTY = "无"


def build_memo(sections: Mapping[str, Sequence[str]]) -> str:
    """每段一行，空段写「无」。"""
    lines = []
    for key in SECTION_KEYS:
        entries = [e.strip() for e in sections.get(key, ()) if e and e.strip()]
        lines.append(f"{SECTION_LABELS[key]}：{ENTRY_SEPARATOR.join(entries) or EMPTY}")
    return "\n".join(lines)


def to_text(days: Sequence[ScheduleDay]) -> str:
    """整周导出：日期 → 工单 → 今日进展、存在问题、明日计划。"""
    lines: list[str] = []
    for day in days:
        lines.append(f"## {day.day}")
        for order in day.orders:
            lines.append(f"### {order.title}")
            progress = order.progress
            if progress is None or not progress.memo:
                lines.append("（未生成）")
                continue
            lines.append(progress.memo)
            if progress.rate is not None or progress.hours is not None:
                suffix = "（估算，可调整）" if progress.source == "ai" else ""
                lines.append(f"工单进展：{progress.rate if progress.rate is not None else '待补齐'}%{suffix}")
                lines.append(f"当日工时：{progress.hours if progress.hours is not None else '待补齐'}h{suffix}")
            lines.append(f"存在问题：{progress.issue or EMPTY}")
            lines.append(f"明日计划：{progress.plan}")
    return "\n".join(lines) + "\n" if lines else ""
