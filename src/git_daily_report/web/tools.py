"""报告的读取与改写操作。

页面按钮与 AI 的工具调用共用这一批函数，两条通道走同一套语义，不会出现点出来
和说出来结果不一致。每次改写立即落盘——rows 文件始终是唯一事实来源。

改写只动文字与归属，不新增事实：明细文本来自提交信息，数字来自素材本身。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

from ..config import Config
from ..linking import matcher
from ..linking.attribution import NEEDS_REVIEW
from ..models import Commit, DayReport, WorkPoint
from ..pipeline import Progress, collect, summarize
from ..render.markdown import review_note, to_markdown
from ..render.memo import to_memo
from ..store import load_days, merge_days, report_path, save_days
from ..text import entries_from_commits
from ..validation import hours_warning


class ToolError(RuntimeError):
    """操作无法完成，原因要能直接说给用户听。"""


class ReportSession:
    """一个时间区间上的报告，以及作用于它的全部操作。"""

    def __init__(self, output_dir: Path, config_dir: Path, since: date, until: date) -> None:
        self._output_dir = output_dir
        self._config_dir = config_dir
        self.since = since
        self.until = until
        self.days: list[DayReport] = load_days(self.path)

    @property
    def path(self) -> Path:
        return report_path(self._output_dir, self.since, self.until)

    def set_period(self, since: date, until: date) -> None:
        self.since, self.until = since, until
        self.days = load_days(self.path)


    def sync(self, cfg: Config, sources: Sequence[str], progress: Progress | None = None) -> str:
        """重新采集并合并进当前报告，改过的内容保留。"""
        result = collect(cfg, self.since, self.until, sources, self._config_dir, progress)
        self.days = merge_days(self.days, result.days)
        self._save()
        return summarize(result, NEEDS_REVIEW)

    def day(self, day: str) -> DayReport:
        for report in self.days:
            if report.day == day:
                return report
        raise ToolError(f"找不到日期 {day}")

    def find_point(self, point_id: str) -> WorkPoint:
        for report in self.days:
            for point in report.points:
                if point.id == point_id:
                    return point
        raise ToolError(f"找不到工作点 {point_id}")

    def set_point(
        self,
        point_id: str,
        *,
        title: str | None = None,
        entries: Sequence[str] | None = None,
        hours: float | None = None,
    ) -> WorkPoint:
        current = self.find_point(point_id)
        updated = replace(
            current,
            title=title if title is not None else current.title,
            entries=tuple(entries) if entries is not None else current.entries,
            hours=hours if hours is not None else current.hours,
        )
        self._put(updated)
        return updated

    def move_commits(
        self,
        point_id: str,
        shas: Sequence[str],
        *,
        to_point_id: str | None = None,
        to_new_title: str | None = None,
    ) -> None:
        """把提交挪到另一个工作点，两侧的明细随之重算。"""
        if not to_point_id and not to_new_title:
            raise ToolError("需要指定目标工作点（to_point_id）或新工作点标题（to_new_title）")

        source = self.find_point(point_id)
        wanted = list(shas)
        moving = [c for c in source.commits if c.sha in wanted]
        missing = sorted(set(wanted) - {c.sha for c in moving})
        if missing:
            raise ToolError(f"工作点 {point_id} 里没有这些提交：{'、'.join(missing)}")

        target = (
            self.find_point(to_point_id)
            if to_point_id
            else self._new_point(source.day, to_new_title or "")
        )
        if target.id == source.id:
            raise ToolError("源与目标是同一个工作点")

        self._put(_with_commits(source, [c for c in source.commits if c.sha not in wanted]))
        self._put(_with_commits(target, list(target.commits) + moving))
        self._prune()

    def merge_points(self, point_ids: Sequence[str], *, title: str | None = None) -> WorkPoint:
        """把多个工作点并成第一个，明细与提交按原顺序拼接。"""
        if len(point_ids) < 2:
            raise ToolError("合并至少需要两个工作点")

        points = [self.find_point(pid) for pid in point_ids]
        head, rest = points[0], points[1:]
        if any(p.day != head.day for p in rest):
            raise ToolError("只能合并同一天的工作点")

        entries: list[str] = list(head.entries)
        commits: list[Commit] = list(head.commits)
        for point in rest:
            entries.extend(e for e in point.entries if e not in entries)
            commits.extend(point.commits)

        merged = replace(
            head,
            title=title or head.title,
            entries=tuple(entries),
            commits=tuple(commits),
            candidates=tuple(dict.fromkeys(sum((list(p.candidates) for p in points), []))),
        )
        for point in rest:
            self._remove(point.id)
        self._put(merged)
        return merged

    def drop_point(self, point_id: str) -> None:
        self.find_point(point_id)
        self._remove(point_id)

    def markdown(self, *, annotate: bool = False) -> str:
        return to_markdown(self.days, annotate=annotate)

    def memo(self) -> str:
        return to_memo(self.days)

    def read_report(self) -> dict:
        """给模型看的紧凑形状：只带判断所需的字段，不塞整份原始素材。"""
        return {
            "range": {"since": self.since.isoformat(), "until": self.until.isoformat()},
            "days": [
                {
                    "day": report.day,
                    "hoursWarning": hours_warning(report.points),
                    "points": [_point_payload(p) for p in report.points],
                }
                for report in self.days
            ],
        }

    def stats(self) -> dict:
        points = [p for d in self.days for p in d.points]
        return {
            "points": len(points),
            "review": sum(1 for p in points if p.confidence in NEEDS_REVIEW),
            "days": len(self.days),
        }

    def _new_point(self, day: str, title: str) -> WorkPoint:
        if not title.strip():
            raise ToolError("新工作点需要一个标题")
        point = WorkPoint(
            id=_free_id(day, title, {p.id for r in self.days for p in r.points}),
            day=day,
            title=title.strip(),
            entries=(),
            confidence="exact",
            generated_title=title.strip(),
        )
        self._put(point)
        return point

    def _put(self, point: WorkPoint) -> None:
        by_day = {report.day: list(report.points) for report in self.days}
        points = by_day.setdefault(point.day, [])
        for index, existing in enumerate(points):
            if existing.id == point.id:
                points[index] = point
                break
        else:
            points.append(point)
        self._rebuild(by_day)

    def _remove(self, point_id: str) -> None:
        by_day = {
            report.day: [p for p in report.points if p.id != point_id] for report in self.days
        }
        self._rebuild(by_day)

    def _prune(self) -> None:
        """既没有提交也没有明细的工作点没有存在意义。"""
        by_day = {
            report.day: [p for p in report.points if p.commits or p.entries]
            for report in self.days
        }
        self._rebuild(by_day)

    def _rebuild(self, by_day: dict[str, list[WorkPoint]]) -> None:
        self.days = [
            DayReport(day=day, points=tuple(by_day[day])) for day in sorted(by_day) if by_day[day]
        ]
        self._save()

    def _save(self) -> None:
        save_days(self.path, self.since, self.until, self.days)


def _with_commits(point: WorkPoint, commits: Sequence[Commit]) -> WorkPoint:
    """换掉提交后重算明细；改写过的文字不动，只刷新生成快照。"""
    generated = entries_from_commits(c.message or c.title for c in commits)
    return replace(
        point,
        commits=tuple(commits),
        entries=point.entries if point.hand_edited else generated,
        generated_entries=generated,
    )


def _free_id(day: str, title: str, taken: Iterable[str]) -> str:
    base = f"{day}/+{matcher.normalize(title) or 'point'}"
    used = set(taken)
    if base not in used:
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if candidate not in used:
            return candidate
    raise ToolError("同名工作点过多，换个标题")


def _point_payload(point: WorkPoint) -> dict:
    return {
        "id": point.id,
        "title": point.title,
        "entries": list(point.entries),
        "hours": point.hours,
        "confidence": point.confidence,
        "note": review_note(point),
        "candidates": list(point.candidates),
        "commits": [
            {"sha": c.sha, "title": c.title, "project": c.project} for c in point.commits
        ],
    }
