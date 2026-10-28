"""领域模型。

以 EOM 日程表为准：某天排了哪些工单，就要给每个工单出一份进展。GitLab 提交
只是写进展的素材，不决定哪天出现哪些块。
"""

from __future__ import annotations

from dataclasses import dataclass
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
    source: str  # eom：EOM 里已填；ai：本工具生成
    problems: tuple[str, ...] = ()  # 校验没过的原因，非空时页面标红

    def to_dict(self) -> dict[str, Any]:
        return {
            "memo": self.memo,
            "issue": self.issue,
            "plan": self.plan,
            "source": self.source,
            "problems": list(self.problems),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Progress:
        return cls(
            memo=data.get("memo", ""),
            issue=data.get("issue", ""),
            plan=data.get("plan", ""),
            source=data.get("source", ""),
            problems=tuple(data.get("problems") or ()),
        )


@dataclass(frozen=True)
class Order:
    """日程表上某天的一个工单。"""

    id: str
    title: str  # 工单标题
    task_title: str  # 任务标题
    project_name: str  # 项目名称
    progress: Progress | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "taskTitle": self.task_title,
            "projectName": self.project_name,
            "progress": self.progress.to_dict() if self.progress else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Order:
        progress = data.get("progress")
        return cls(
            id=str(data.get("id", "")),
            title=data.get("title", ""),
            task_title=data.get("taskTitle", ""),
            project_name=data.get("projectName", ""),
            progress=Progress.from_dict(progress) if isinstance(progress, dict) else None,
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
