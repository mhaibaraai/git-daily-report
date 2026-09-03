"""Markdown 视图：日期 → 工作点 → 明细。

默认输出干净文本，直接复制粘贴即可；预览时用 annotate 打开归属标记，把需要
复核的工作点标出来。标记只进预览，不进复制出去的正文。
"""

from __future__ import annotations

from typing import Sequence

from ..linking.attribution import AMBIGUOUS, EXACT, FUZZY, SOLE, UNMATCHED
from ..models import DayReport, WorkPoint

_MARKERS = {
    FUZZY: "⚠ 按标题推断",
    SOLE: "⚠ 当天唯一任务",
    UNMATCHED: "⚠ 未关联",
}


def review_note(point: WorkPoint) -> str:
    """工作点的归属标记，exact 无标记。"""
    if point.confidence == AMBIGUOUS:
        listed = " / ".join(point.candidates)
        return f"⚠ 归属存疑：{listed}" if listed else "⚠ 归属存疑"
    if point.confidence == EXACT:
        return ""
    return _MARKERS.get(point.confidence, "")


def to_markdown(days: Sequence[DayReport], *, annotate: bool = False) -> str:
    lines: list[str] = []
    for report in days:
        if not report.points:
            continue
        lines.append(f"## {report.day}")
        for point in report.points:
            note = review_note(point) if annotate else ""
            lines.append(f"### {point.title} {note}".rstrip())
            lines.extend(f"- {entry}" for entry in point.entries)
    return "\n".join(lines) + "\n" if lines else ""
