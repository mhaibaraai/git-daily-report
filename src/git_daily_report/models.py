"""领域模型。

以 EOM 日程表为准：某天排了哪些工单，就要给每个工单出一份进展。GitLab 提交
只是写进展的素材，不决定哪天出现哪些块。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any


@dataclass(frozen=True)
class Commit:
    """GitLab 上的一条提交。"""

    project: str
    sha: str
    day: str  # YYYY-MM-DD
    title: str
    message: str
    web_url: str


@dataclass(frozen=True)
class Progress:
    """一个工单在某天的进展，字段与 EOM 填报弹窗一一对应。"""

    memo: str  # 三段式「今日进展」
    issue: str  # 存在问题
    plan: str  # 明日计划
    source: str  # eom：EOM 里已填；ai：本工具生成；amend：已填日报待修正百分比
    problems: tuple[str, ...] = ()  # 校验没过的原因，非空时页面标红
    rate: float | None = None  # 工单累计完成百分比
    hours: float | None = None  # 当日实际工时；工具生成值为估算
    report_id: str = ""  # EOM 已填日报 ID，修正时按它覆盖
    original_rate: float | None = None  # 修正前 EOM 里的百分比

    def to_dict(self) -> dict[str, Any]:
        return {
            "memo": self.memo,
            "issue": self.issue,
            "plan": self.plan,
            "source": self.source,
            "problems": list(self.problems),
            "rate": _json_number(self.rate),
            "hours": _json_number(self.hours),
            "reportId": self.report_id,
            "originalRate": _json_number(self.original_rate),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Progress:
        return cls(
            memo=data.get("memo", ""),
            issue=data.get("issue", ""),
            plan=data.get("plan", ""),
            source=data.get("source", ""),
            problems=tuple(data.get("problems") or ()),
            rate=data.get("rate"),
            hours=data.get("hours"),
            report_id=str(data.get("reportId") or ""),
            original_rate=data.get("originalRate"),
        )


@dataclass(frozen=True)
class Order:
    """日程表上某天的一个工单。"""

    id: str
    title: str  # 工单标题
    task_title: str  # 任务标题
    project_name: str  # 项目名称
    progress: Progress | None = None
    rate_min: float | None = None  # 更早已填日报的最高累计进展
    rate_max: float | None = None  # 更晚已填日报的最低累计进展
    previous_day: str = ""  # 同一工单 ID 最近已填日报日期
    previous_memo: str = ""  # 最近已填进展，供后续内容衔接
    continuity_id: str = ""  # 旧报告兼容字段，不用于合并不同工单 ID

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "taskTitle": self.task_title,
            "projectName": self.project_name,
            "progress": self.progress.to_dict() if self.progress else None,
            "rateMin": _json_number(self.rate_min),
            "rateMax": _json_number(self.rate_max),
            "previousDay": self.previous_day,
            "previousMemo": self.previous_memo,
            "continuityId": self.continuity_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Order:
        progress = data.get("progress")
        order_id = str(data.get("id", ""))
        continuity_id = str(data.get("continuityId") or "")
        foreign_context = bool(continuity_id and continuity_id != order_id)
        return cls(
            id=order_id,
            title=data.get("title", ""),
            task_title=data.get("taskTitle", ""),
            project_name=data.get("projectName", ""),
            progress=Progress.from_dict(progress) if isinstance(progress, dict) else None,
            rate_min=None if foreign_context else data.get("rateMin"),
            rate_max=None if foreign_context else data.get("rateMax"),
            previous_day="" if foreign_context else data.get("previousDay", ""),
            previous_memo="" if foreign_context else data.get("previousMemo", ""),
            continuity_id="" if foreign_context else continuity_id,
        )


@dataclass(frozen=True)
class ScheduleDay:
    """日程表上的一天。节假日与调休休息日没有工单，自然不会出现。"""

    day: str  # YYYY-MM-DD
    orders: tuple[Order, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"day": self.day, "orders": [o.to_dict() for o in self.orders]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduleDay:
        return cls(
            day=data.get("day", ""),
            orders=tuple(
                Order.from_dict(o) for o in (data.get("orders") or []) if isinstance(o, dict)
            ),
        )


def _json_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if isinstance(value, int) or isfinite(value) else None
