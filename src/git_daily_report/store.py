"""报告的读写与合并。

rows 文件是唯一事实来源，Markdown 与三段式都只是它的单向视图。重跑采集默认
合并——人工或 AI 改过的标题与明细一律保留，只有 force 才覆盖。
"""

from __future__ import annotations

import calendar
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

from .models import DayReport, WorkPoint

SCHEMA_VERSION = 2


def report_path(output_dir: Path, since: date, until: date) -> Path:
    """整月用 YYYY-MM 命名，其余保留两端边界。"""
    last = calendar.monthrange(since.year, since.month)[1]
    if since.day == 1 and until == date(since.year, since.month, last):
        return output_dir / f"{since.year:04d}-{since.month:02d}.json"
    return output_dir / f"{since.isoformat()}_{until.isoformat()}.json"


def load_days(path: Path) -> list[DayReport]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    days = payload.get("days") if isinstance(payload, dict) else None
    if not isinstance(days, list):
        return []
    return [DayReport.from_dict(d) for d in days if isinstance(d, dict)]


def save_days(path: Path, since: date, until: date, days: Sequence[DayReport]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "range": {"since": since.isoformat(), "until": until.isoformat()},
        "days": [d.to_dict() for d in days],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def merge_days(
    existing: Iterable[DayReport],
    generated: Iterable[DayReport],
    *,
    force: bool = False,
) -> list[DayReport]:
    """用新采集的结果刷新报告，保留改过的内容。"""
    old = {point.identity: point for report in existing for point in report.points}
    merged: dict[str, list[WorkPoint]] = {}

    for report in generated:
        for point in report.points:
            previous = old.pop(point.identity, None)
            merged.setdefault(report.day, []).append(_merge_point(previous, point, force=force))

    # 新一轮采集里消失的工作点保留下来，采集范围变了不该让已有内容悄悄消失
    for point in old.values():
        merged.setdefault(point.day, []).append(point)

    return [DayReport(day=day, points=tuple(merged[day])) for day in sorted(merged)]


def _merge_point(previous: WorkPoint | None, fresh: WorkPoint, *, force: bool) -> WorkPoint:
    if previous is None or force:
        return fresh
    if previous.hand_edited:
        # 保留改过的文字，但同步刷新素材与生成快照，以便下次仍能识别改动
        return replace(
            previous,
            commits=fresh.commits,
            confidence=fresh.confidence,
            candidates=fresh.candidates,
            generated_title=fresh.generated_title,
            generated_entries=fresh.generated_entries,
        )
    return replace(fresh, hours=fresh.hours if fresh.hours is not None else previous.hours)
