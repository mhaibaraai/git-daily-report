"""三段式 memo 视图。

段名复刻自 EOM 前端 Schedule 页面的实现，保证粘贴回去时能被前端正确识别。
这是单向导出——页面上编辑的是工作点，没有把 memo 文本读回来的通道。
"""

from __future__ import annotations

import re
from typing import Mapping, Sequence

from ..models import DayReport, WorkPoint
from ..text import clean_entry

SECTION_KEYS = ("task", "data", "problem")

SECTION_LABELS: Mapping[str, str] = {
    "task": "具体任务",
    "data": "关键数据",
    "problem": "问题解决过程",
}


ENTRY_SEPARATOR = "；"


# 判定一条描述是否属于「问题解决过程」
PROBLEM_KEYWORDS = re.compile(
    r"问题|异常|故障|报错|错误|缺陷|定位|排查|复现|修复|解决|处理|调整|优化|回归|验证|漏洞|阻塞"
)



def _append(bucket: list[str], value: str) -> None:
    """清理后去重追加，空值丢弃。"""
    cleaned = clean_entry(value)
    if cleaned and cleaned not in bucket:
        bucket.append(cleaned)



def build_memo(sections: Mapping[str, Sequence[str]]) -> str:
    """按前端口径序列化：每段一行，条目用「；」连接。"""
    lines = []
    for key in SECTION_KEYS:
        entries = [e.strip() for e in sections.get(key, ()) if e and e.strip()]
        lines.append(f"{SECTION_LABELS[key]}：{ENTRY_SEPARATOR.join(entries)}")
    return "\n".join(lines)



def sections_for_point(point: WorkPoint) -> dict[str, list[str]]:
    """把一个工作点拆成三段。

    明细按是否描述问题分流；关键数据只写提交量这类能从素材直接数出来的事实。
    抽不出的段落留空，不做任何推测填充。
    """
    task: list[str] = []
    problem: list[str] = []
    for entry in point.entries:
        _append(problem if PROBLEM_KEYWORDS.search(entry) else task, entry)

    data: list[str] = []
    if point.commits:
        projects = sorted({c.project for c in point.commits if c.project})
        scope = "、".join(projects)
        count = len(point.commits)
        _append(data, f"{scope} 提交 {count} 次" if scope else f"提交 {count} 次")

    return {"task": task, "data": data, "problem": problem}


def to_memo(days: Sequence[DayReport]) -> str:
    """三段式视图，供直接粘贴回 EOM 日程表页面。"""
    lines: list[str] = []
    for report in days:
        if not report.points:
            continue
        lines.append(f"## {report.day}")
        for point in report.points:
            lines.append(f"### {point.title}")
            lines.append(build_memo(sections_for_point(point)))
            if point.hours is not None:
                lines.append(f"工时：{point.hours}")
    return "\n".join(lines) + "\n" if lines else ""
