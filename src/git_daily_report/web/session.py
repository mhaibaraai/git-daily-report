"""一个时间区间上的周报。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ..config import Config
from ..pipeline import Progress, run, summarize
from ..render.memo import to_text
from ..store import load_days, report_path, save_days


class ReportSession:
    def __init__(self, output_dir: Path, since: date, until: date) -> None:
        self._output_dir = output_dir
        self.since = since
        self.until = until
        self.days = load_days(self.path)

    @property
    def path(self) -> Path:
        return report_path(self._output_dir, self.since, self.until)

    def set_period(self, since: date, until: date) -> None:
        self.since, self.until = since, until
        self.days = load_days(self.path)

    def sync(self, cfg: Config, progress: Progress | None = None) -> str:
        """重新拉取并生成，结果直接覆盖。"""
        result = run(cfg, self.since, self.until, progress)
        self.days = result.days
        save_days(self.path, self.since, self.until, self.days)
        return summarize(result)

    def text(self) -> str:
        return to_text(self.days)

    def state(self) -> dict:
        return {
            "range": {"since": self.since.isoformat(), "until": self.until.isoformat()},
            "days": [d.to_dict() for d in self.days],
        }
