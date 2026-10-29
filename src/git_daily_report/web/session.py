"""一个时间区间上的周报。"""

from __future__ import annotations

from dataclasses import replace
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
        from ..locking import report_lock

        with report_lock(self.path):
            result = run(cfg, self.since, self.until, progress)
            self.days = result.days
            save_days(self.path, self.since, self.until, self.days)
        return summarize(result)

    def update_progress(self, day: str, order_id: str, rate: object, hours: object) -> None:
        """只调整本地估算数值，重新检查整份报告后保存。"""
        from ..locking import report_lock
        from ..validate import validate_days

        rate_value = _number(rate, "工单进展", 100)
        hours_value = _number(hours, "工时", 7.5)
        with report_lock(self.path):
            days = load_days(self.path)
            found = False
            changed = []
            for current in days:
                orders = []
                for order in current.orders:
                    if current.day == day and order.id == order_id:
                        if order.progress is None:
                            raise ValueError("请先生成工单进展")
                        order = replace(order, progress=_adjusted(order.progress, rate_value, hours_value))
                        found = True
                    orders.append(order)
                changed.append(replace(current, orders=tuple(orders)))
            if not found:
                raise ValueError("没有找到该日期的工单")
            self.days = validate_days(changed)
            save_days(self.path, self.since, self.until, self.days)

    def submit(self, cfg: Config, *, retry_unknown: bool = False) -> dict:
        from ..submit import submit_report

        cfg.require(need_ai=False, need_gitlab=False)
        try:
            return submit_report(
                cfg.eom, self.path, self.since, self.until, retry_unknown=retry_unknown,
            )
        finally:
            self.days = load_days(self.path)

    def text(self) -> str:
        self.days = load_days(self.path)
        return to_text(self.days)

    def state(self) -> dict:
        self.days = load_days(self.path)
        return {
            "range": {"since": self.since.isoformat(), "until": self.until.isoformat()},
            "days": [d.to_dict() for d in self.days],
        }


def _adjusted(progress, rate: float, hours: float):
    """生成块直接改数值；已填日报只能改百分比，与原值不同即标为待修正。"""
    from ..validate import number

    if progress.source == "ai":
        return replace(progress, rate=rate, hours=hours)
    if progress.source not in {"eom", "amend"} or not progress.report_id:
        raise ValueError("EOM 已填日报不能在本地修改")
    if number(hours) != number(progress.hours):
        raise ValueError("修正已填日报只能调整工单进展，工时保持 EOM 原值")
    original = progress.original_rate if progress.source == "amend" else progress.rate
    if number(rate) == number(original):
        return replace(progress, source="eom", rate=original, original_rate=None, problems=())
    return replace(progress, source="amend", rate=rate, original_rate=original)


def _number(value: object, label: str, maximum: float) -> float:
    from ..validate import number

    parsed = number(value)
    if parsed is None or not 0 <= parsed <= maximum:
        raise ValueError(f"{label}必须是 0–{maximum} 之间的有限数字")
    return value
