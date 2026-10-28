"""周报文件的读写。

保存一次拉取的排期与生成结果，重开页面不必重新调 AI；重新生成直接覆盖。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Sequence

from .models import ScheduleDay

SCHEMA_VERSION = 3


def report_path(output_dir: Path, since: date, until: date) -> Path:
    """整周（周一至周日）用 ISO 周号命名，其余保留两端边界。"""
    if since.weekday() == 0 and until == since + timedelta(days=6):
        year, week, _ = since.isocalendar()
        return output_dir / f"{year:04d}-W{week:02d}.json"
    return output_dir / f"{since.isoformat()}_{until.isoformat()}.json"


def load_days(path: Path) -> list[ScheduleDay]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or payload.get("schemaVersion") != SCHEMA_VERSION:
        return []
    days = payload.get("days")
    if not isinstance(days, list):
        return []
    return [ScheduleDay.from_dict(d) for d in days if isinstance(d, dict)]


def save_days(path: Path, since: date, until: date, days: Sequence[ScheduleDay]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "range": {"since": since.isoformat(), "until": until.isoformat()},
        "days": [d.to_dict() for d in days],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
