"""把工作项与提交归并成「日期 → 工作点 → 明细」三层结构。"""

from __future__ import annotations

from datetime import date

from git_daily_report.linking.attribution import AMBIGUOUS, EXACT, UNMATCHED
from git_daily_report.models import Commit, WorkItem
from git_daily_report.report import build_days

SINCE, UNTIL = date(2026, 8, 1), date(2026, 8, 31)


def item(number: str, title: str, program: str | None, created: str, finished: str | None = None):
    return WorkItem.from_api(
        {
            "number": number,
            "title": title,
            "state": "closed" if finished else "open",
            "created_at": f"{created}T09:00:00+08:00",
            "updated_at": f"{finished or created}T18:00:00+08:00",
            "finished_at": f"{finished}T18:00:00+08:00" if finished else None,
            "deadline": None,
            "program": {"name": program} if program else None,
            "html_url": "",
        }
    )


def commit(project: str, title: str, day: str, sha: str = "s"):
    return Commit(project=project, sha=sha, day=day, title=title, message=title, web_url="")


def test_builds_day_then_point_then_entries():
    items = [item("A", "态势图层重构", "应急平台", "2026-07-25")]
    commits = [
        commit("g/应急平台", "feat: 完成图层切换逻辑", "2026-08-10", "1"),
        commit("g/应急平台", "fix: 修复图例渲染错位", "2026-08-10", "2"),
    ]
    days = build_days(items, commits, SINCE, UNTIL, {})

    assert [d.day for d in days] == ["2026-08-10"]
    point = days[0].points[0]
    assert point.title == "态势图层重构"
    assert point.entries == ("完成图层切换逻辑", "修复图例渲染错位")
    assert point.confidence == EXACT
    assert point.item_number == "A"


def test_merge_commits_are_dropped_and_duplicates_collapse():
    items = [item("A", "任务", "项目甲", "2026-07-25")]
    commits = [
        commit("g/项目甲", "Merge branch 'dev' into main", "2026-08-10", "1"),
        commit("g/项目甲", "feat: 同一件事", "2026-08-10", "2"),
        commit("g/项目甲", "feat(scope): 同一件事", "2026-08-10", "3"),
    ]
    days = build_days(items, commits, SINCE, UNTIL, {})

    assert days[0].points[0].entries == ("同一件事",)
    # 提交本身不丢，只是不进明细文本
    assert len(days[0].points[0].commits) == 3


def test_unmatched_commits_become_their_own_point_and_sort_last():
    items = [
        item("A", "任务甲", "项目甲", "2026-07-25"),
        item("B", "任务乙", "项目乙", "2026-07-25"),
    ]
    commits = [
        commit("g/项目丙", "feat: 无主", "2026-08-10", "1"),
        commit("g/项目甲", "feat: 有主", "2026-08-10", "2"),
    ]
    days = build_days(items, commits, SINCE, UNTIL, {})
    points = days[0].points

    assert [p.title for p in points] == ["任务甲", "g/项目丙"]
    assert points[1].confidence == UNMATCHED
    assert points[1].item_number is None


def test_point_carries_the_confidence_that_most_needs_review():
    """同一工作点里只要有一条存疑，整个工作点就该被标出来。"""
    items = [
        item("A", "任务甲", "项目甲", "2026-07-25", finished="2026-08-20"),
        item("B", "任务乙", "项目甲", "2026-08-09", finished="2026-08-11"),
    ]
    commits = [
        commit("g/项目甲", "feat: 任务乙的活", "2026-08-10", "1"),
        commit("g/项目甲", "chore: 说不清", "2026-08-10", "2"),
    ]
    days = build_days(items, commits, SINCE, UNTIL, {})
    day = next(d for d in days if d.day == "2026-08-10")
    point = next(p for p in day.points if p.item_number == "B")

    assert point.confidence == AMBIGUOUS
    assert set(point.candidates) == {"A", "B"}


def test_finishing_an_item_produces_a_point_even_without_commits():
    items = [item("A", "任务甲", "项目甲", "2026-08-03", finished="2026-08-05")]
    days = build_days(items, [], SINCE, UNTIL, {})

    assert [d.day for d in days] == ["2026-08-05"]
    assert days[0].points[0].entries == ("工作项完成",)


def test_creating_an_item_is_not_work_and_makes_no_point():
    """建单常常是项目经理或接手动作，那天未必做了事，留下只会淹没真正的内容。"""
    items = [item("A", "任务甲", "项目甲", "2026-08-03")]
    assert build_days(items, [], SINCE, UNTIL, {}) == []


def test_event_marker_appends_to_a_day_that_already_has_commits():
    items = [item("A", "任务甲", "项目甲", "2026-07-25", finished="2026-08-10")]
    commits = [commit("g/项目甲", "feat: 收尾", "2026-08-10", "1")]
    days = build_days(items, commits, SINCE, UNTIL, {})
    point = next(p for p in days if p.day == "2026-08-10").points[0]

    assert point.entries == ("收尾", "工作项完成")


def test_days_outside_the_range_are_excluded():
    items = [item("A", "任务", "项目甲", "2026-07-20", finished="2026-09-05")]
    commits = [commit("g/项目甲", "feat: 界内", "2026-08-10", "1")]
    days = build_days(items, commits, SINCE, UNTIL, {})

    assert [d.day for d in days] == ["2026-08-10"]


def test_generated_snapshot_matches_content_so_edits_are_detectable():
    items = [item("A", "任务", "项目甲", "2026-07-25")]
    days = build_days(items, [commit("g/项目甲", "feat: 甲", "2026-08-10")], SINCE, UNTIL, {})
    point = days[0].points[0]

    assert point.generated_title == point.title
    assert point.generated_entries == point.entries
    assert not point.hand_edited


def test_point_ids_are_stable_and_unique_within_a_day():
    items = [item("A", "任务甲", "项目甲", "2026-07-25")]
    commits = [
        commit("g/项目甲", "feat: 甲", "2026-08-10", "1"),
        commit("g/项目丙", "feat: 无主", "2026-08-10", "2"),
        commit("g/项目丁", "feat: 也无主", "2026-08-10", "3"),
    ]
    days = build_days(items, commits, SINCE, UNTIL, {})
    ids = [p.id for p in days[0].points]

    assert len(set(ids)) == len(ids)
    assert ids[0] == "2026-08-10/A"
    assert all(i.startswith("2026-08-10/") for i in ids)
