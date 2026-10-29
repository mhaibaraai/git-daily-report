"""周报文件的命名与读写。"""

from __future__ import annotations

from datetime import date

from git_daily_report import store
from git_daily_report.models import Order, Progress, ScheduleDay


def test_a_full_week_is_named_by_iso_week(tmp_path):
    path = store.report_path(tmp_path, date(2026, 10, 26), date(2026, 11, 1))
    assert path.name == "2026-W44.json"


def test_other_ranges_keep_both_bounds(tmp_path):
    path = store.report_path(tmp_path, date(2026, 10, 26), date(2026, 10, 30))
    assert path.name == "2026-10-26_2026-10-30.json"


def test_round_trip_keeps_orders_and_progress(tmp_path):
    progress = Progress(memo="具体任务：完成筛选", issue="无", plan="联调", source="ai", problems=("含代码词：dev",))
    days = [
        ScheduleDay(
            "2026-10-26",
            (Order("1", "订单列表筛选优化", "任务", "项目", progress), Order("2", "报表数据梳理", "任务", "项目", None)),
        )
    ]
    path = tmp_path / "w.json"
    store.save_days(path, date(2026, 10, 26), date(2026, 11, 1), days)

    assert store.load_days(path) == days


def test_missing_or_broken_file_reads_as_empty(tmp_path):
    assert store.load_days(tmp_path / "none.json") == []
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert store.load_days(broken) == []


def test_failed_atomic_save_preserves_existing_report(tmp_path, monkeypatch):
    import os
    import pytest
    path = tmp_path / "report.json"
    original = [ScheduleDay("2026-10-26", (Order("1", "原内容", "任务", "项目"),))]
    store.save_days(path, date(2026, 10, 26), date(2026, 11, 1), original)
    def failed_replace(*args):
        raise OSError("模拟写入失败")
    monkeypatch.setattr(os, "replace", failed_replace)
    with pytest.raises(OSError):
        store.save_days(path, date(2026, 10, 26), date(2026, 11, 1), [])
    assert store.load_days(path) == original
