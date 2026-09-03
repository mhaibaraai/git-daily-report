"""v2 报告的读写与合并。

rows 文件是唯一事实来源，Markdown 只是它的单向视图。重跑采集默认不覆盖人工
或 AI 改过的内容，只有显式 force 才覆盖。
"""

from __future__ import annotations

from datetime import date

from git_daily_report import store
from git_daily_report.models import Commit, DayReport, WorkPoint


def point(point_id: str, title: str, entries: tuple[str, ...], *, generated: bool = True, **kw):
    return WorkPoint(
        id=point_id,
        day=point_id.split("/", 1)[0],
        title=title,
        entries=entries,
        confidence=kw.pop("confidence", "exact"),
        item_number=kw.pop("item_number", "A"),
        commits=kw.pop("commits", ()),
        hours=kw.pop("hours", None),
        generated_title=title if generated else kw.pop("generated_title", ""),
        generated_entries=entries if generated else kw.pop("generated_entries", ()),
        **kw,
    )


def day(day_str: str, *points: WorkPoint) -> DayReport:
    return DayReport(day=day_str, points=points)


def test_month_range_gets_a_month_shaped_filename(tmp_path):
    path = store.report_path(tmp_path, date(2026, 8, 1), date(2026, 8, 31))
    assert path.name == "2026-08.json"


def test_arbitrary_range_keeps_both_bounds_in_the_filename(tmp_path):
    path = store.report_path(tmp_path, date(2026, 8, 4), date(2026, 8, 10))
    assert path.name == "2026-08-04_2026-08-10.json"


def test_round_trip_preserves_points_and_commits(tmp_path):
    path = tmp_path / "r.json"
    commit = Commit("g/p", "sha1", "2026-08-10", "标题", "标题", "http://x/commit/sha1")
    days = [day("2026-08-10", point("2026-08-10/A", "任务", ("甲", "乙"), commits=(commit,)))]

    store.save_days(path, date(2026, 8, 1), date(2026, 8, 31), days)
    loaded = store.load_days(path)

    assert loaded[0].points[0].entries == ("甲", "乙")
    assert loaded[0].points[0].commits[0].sha == "sha1"
    assert loaded[0].points[0].commits[0].web_url.endswith("sha1")


def test_missing_or_broken_file_reads_as_empty(tmp_path):
    assert store.load_days(tmp_path / "nope.json") == []
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    assert store.load_days(broken) == []


def test_merge_keeps_hand_edited_text_but_refreshes_material():
    edited = point("2026-08-10/A", "我改过的标题", ("我写的明细",), generated=False,
                   generated_title="原标题", generated_entries=("原明细",))
    commit = Commit("g/p", "new", "2026-08-10", "新提交", "新提交", "")
    fresh = point("2026-08-10/A", "原标题", ("原明细", "新提交"), commits=(commit,))

    merged = store.merge_days([day("2026-08-10", edited)], [day("2026-08-10", fresh)])
    result = merged[0].points[0]

    assert result.title == "我改过的标题"
    assert result.entries == ("我写的明细",)
    assert result.commits[0].sha == "new"
    assert result.generated_entries == ("原明细", "新提交")


def test_force_discards_hand_edits():
    edited = point("2026-08-10/A", "我改过的", ("我的",), generated=False,
                   generated_title="原", generated_entries=("原明细",))
    fresh = point("2026-08-10/A", "原", ("原明细",))

    merged = store.merge_days([day("2026-08-10", edited)], [day("2026-08-10", fresh)], force=True)
    assert merged[0].points[0].title == "原"


def test_untouched_point_takes_fresh_content_but_keeps_filled_hours():
    old = point("2026-08-10/A", "任务", ("甲",), hours=2.5)
    fresh = point("2026-08-10/A", "任务", ("甲", "乙"))

    merged = store.merge_days([day("2026-08-10", old)], [day("2026-08-10", fresh)])
    assert merged[0].points[0].entries == ("甲", "乙")
    assert merged[0].points[0].hours == 2.5


def test_points_that_vanished_from_the_new_run_are_kept():
    """采集范围变了不该让已有内容悄悄消失。"""
    old = point("2026-08-09/B", "旧任务", ("旧明细",))
    fresh = point("2026-08-10/A", "新任务", ("新明细",))

    merged = store.merge_days([day("2026-08-09", old)], [day("2026-08-10", fresh)])

    assert [d.day for d in merged] == ["2026-08-09", "2026-08-10"]
