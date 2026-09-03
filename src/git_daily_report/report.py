"""把工作项与提交归并成「日期 → 工作点 → 明细」三层结构。

主干是 git 活动，不依赖任何工单：某天有提交打到某个工作项，它才在那天出现。
工作项自身的创建与完成也各留一条记录，避免那天只有任务流转、没有代码时报告
是空的。归不上的提交按项目聚成独立工作点，不静默丢弃。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Mapping, Sequence

from .linking.attribution import (
    AMBIGUOUS,
    EXACT,
    FUZZY,
    SOLE,
    UNMATCHED,
    attribute_day,
)
from .linking.matcher import normalize
from .models import Commit, DayReport, WorkItem, WorkPoint
from .text import dedupe, entries_from_commits

# 越靠后越需要复核，同一工作点取其中最需要复核的那档
_SEVERITY = {EXACT: 0, FUZZY: 1, SOLE: 2, AMBIGUOUS: 3, UNMATCHED: 4}


@dataclass
class _Bucket:
    """一个工作点在归并过程中的中间态。"""

    point_id: str
    title: str
    item_number: str | None = None
    commits: list[Commit] = field(default_factory=list)
    confidences: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    extra_entries: list[str] = field(default_factory=list)

    def confidence(self) -> str:
        if not self.confidences:
            return EXACT
        return max(self.confidences, key=lambda level: _SEVERITY.get(level, 0))

    def to_point(self) -> WorkPoint:
        entries = dedupe(
            entries_from_commits(c.message or c.title for c in self.commits)
            + tuple(self.extra_entries)
        )
        return WorkPoint(
            id=self.point_id,
            day=self.point_id.split("/", 1)[0],
            title=self.title,
            entries=entries,
            confidence=self.confidence(),
            item_number=self.item_number,
            candidates=tuple(dedupe(self.candidates)),
            commits=tuple(self.commits),
            generated_title=self.title,
            generated_entries=entries,
        )


def build_days(
    items: Sequence[WorkItem],
    commits: Sequence[Commit],
    since: date,
    until: date,
    mapping: Mapping[str, str] | None = None,
) -> list[DayReport]:
    """归并出区间内每一天的工作点。"""
    mapping = mapping or {}
    buckets: dict[str, dict[str, _Bucket]] = {}

    for day, day_commits in _commits_by_day(commits, since, until).items():
        of_day = buckets.setdefault(day, {})
        for attribution in attribute_day(items, day_commits, date.fromisoformat(day), mapping):
            bucket = _bucket_for(of_day, day, attribution.item, attribution.commit)
            bucket.commits.append(attribution.commit)
            bucket.confidences.append(attribution.confidence)
            bucket.candidates.extend(attribution.candidates)

    _add_item_events(buckets, items, since, until)

    return [
        DayReport(day=day, points=tuple(_ordered_points(buckets[day])))
        for day in sorted(buckets)
    ]


def _commits_by_day(
    commits: Iterable[Commit], since: date, until: date
) -> dict[str, list[Commit]]:
    low, high = since.isoformat(), until.isoformat()
    grouped: dict[str, list[Commit]] = {}
    for commit in commits:
        if low <= commit.day <= high:
            grouped.setdefault(commit.day, []).append(commit)
    return grouped


def _bucket_for(
    of_day: dict[str, _Bucket], day: str, item: WorkItem | None, commit: Commit
) -> _Bucket:
    """归不上的提交按所在项目单独成点，标题就用项目名。"""
    if item is not None:
        return _ensure(of_day, f"{day}/{item.number}", item.title, item.number)
    return _ensure(of_day, f"{day}/~{normalize(commit.project)}", commit.project, None)


def _ensure(
    of_day: dict[str, _Bucket], point_id: str, title: str, number: str | None
) -> _Bucket:
    if point_id not in of_day:
        of_day[point_id] = _Bucket(point_id=point_id, title=title, item_number=number)
    return of_day[point_id]


def _add_item_events(
    buckets: dict[str, dict[str, _Bucket]],
    items: Sequence[WorkItem],
    since: date,
    until: date,
) -> None:
    """工作项的创建与完成各留一条记录，当天已有提交时并入同一个工作点。"""
    low, high = since.isoformat(), until.isoformat()
    for item in items:
        for day, label in item.event_days():
            if not (low <= day <= high):
                continue
            bucket = _ensure(
                buckets.setdefault(day, {}), f"{day}/{item.number}", item.title, item.number
            )
            bucket.extra_entries.append(label)


def _ordered_points(of_day: Mapping[str, _Bucket]) -> list[WorkPoint]:
    """有主的工作点在前，按提交数降序；归不上的排在最后。"""
    points = [bucket.to_point() for bucket in of_day.values()]
    points.sort(key=lambda p: (p.item_number is None, -len(p.commits), p.title))
    return points
