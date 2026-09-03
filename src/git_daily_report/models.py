"""领域模型。

工作项是跨天的任务容器，提交是落在某一天的明细，工作点是两者在某一天的交集。
WorkPoint 同时存生成值与当前值，靠两者是否一致识别人工或 AI 的改动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

def parse_day(value: str | None) -> date | None:
    """把接口返回的 ISO 时间戳取成日期，取不到返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


@dataclass(frozen=True)
class WorkItem:
    """Gitee 企业工作项。"""

    number: str
    title: str
    state: str
    created_at: str
    updated_at: str
    finished_at: str | None
    deadline: str | None
    program_name: str | None
    html_url: str

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> WorkItem:
        program = raw.get("program") or {}
        return cls(
            number=str(raw.get("number") or ""),
            title=(raw.get("title") or "").strip(),
            state=raw.get("state") or "",
            created_at=raw.get("created_at") or "",
            updated_at=raw.get("updated_at") or "",
            finished_at=raw.get("finished_at"),
            deadline=raw.get("deadline"),
            program_name=(program.get("name") or None) if isinstance(program, dict) else None,
            html_url=raw.get("html_url") or "",
        )

    @property
    def started_on(self) -> date | None:
        return parse_day(self.created_at)

    @property
    def finished_on(self) -> date | None:
        return parse_day(self.finished_at)

    def is_active_on(self, day: date) -> bool:
        """工作项是跨天容器：从创建当天起算，未完成的一直有效。"""
        start = self.started_on
        if start is None or day < start:
            return False
        end = self.finished_on
        return end is None or day <= end

    def span_days(self, day: date) -> int:
        """截至 day 的活跃跨度，用于在多个候选里优先取窗口更窄的那个。"""
        start = self.started_on
        if start is None:
            return 0
        end = self.finished_on or day
        return max((end - start).days, 0)

    def event_days(self) -> tuple[tuple[str, str], ...]:
        """工作项自身的节点日。

        只算完成——关闭任务是实打实的成果。创建往往是项目经理建单或自己接手，
        那天未必做了事，留下只会把真正的内容淹掉。
        """
        end = self.finished_on
        return ((end.isoformat(), "工作项完成"),) if end else ()


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
class WorkPoint:
    """某一天的一个工作点：一个 Gitee 工作项，或一组归不上的提交。"""

    id: str
    day: str  # YYYY-MM-DD
    title: str
    entries: tuple[str, ...]
    confidence: str
    item_number: str | None = None
    candidates: tuple[str, ...] = ()
    commits: tuple[Commit, ...] = ()
    hours: float | None = None
    generated_title: str = ""
    generated_entries: tuple[str, ...] = ()

    @property
    def hand_edited(self) -> bool:
        """标题或明细与当初生成的值不一致，说明被人工或 AI 改过。"""
        return self.title != self.generated_title or self.entries != self.generated_entries

    @property
    def identity(self) -> tuple[str, str]:
        return (self.day, self.id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "day": self.day,
            "title": self.title,
            "entries": list(self.entries),
            "confidence": self.confidence,
            "itemNumber": self.item_number,
            "candidates": list(self.candidates),
            "commits": [
                {
                    "project": c.project,
                    "sha": c.sha,
                    "title": c.title,
                    "url": c.web_url,
                }
                for c in self.commits
            ],
            "hours": self.hours,
            "generatedTitle": self.generated_title,
            "generatedEntries": list(self.generated_entries),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkPoint:
        day = data.get("day", "")
        return cls(
            id=data.get("id", ""),
            day=day,
            title=data.get("title", ""),
            entries=tuple(data.get("entries") or ()),
            confidence=data.get("confidence", ""),
            item_number=data.get("itemNumber"),
            candidates=tuple(data.get("candidates") or ()),
            commits=tuple(
                Commit(
                    project=c.get("project", ""),
                    sha=c.get("sha", ""),
                    day=day,
                    title=c.get("title", ""),
                    message=c.get("title", ""),
                    web_url=c.get("url", ""),
                )
                for c in (data.get("commits") or [])
                if isinstance(c, dict)
            ),
            hours=data.get("hours"),
            generated_title=data.get("generatedTitle", ""),
            generated_entries=tuple(data.get("generatedEntries") or ()),
        )


@dataclass(frozen=True)
class DayReport:
    """某一天的全部工作点。"""

    day: str  # YYYY-MM-DD
    points: tuple[WorkPoint, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"day": self.day, "points": [p.to_dict() for p in self.points]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DayReport:
        return cls(
            day=data.get("day", ""),
            points=tuple(
                WorkPoint.from_dict(p) for p in (data.get("points") or []) if isinstance(p, dict)
            ),
        )
