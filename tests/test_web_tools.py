"""报告的改写操作。

页面按钮与 AI 工具调用共用同一批函数，避免两条通道行为不一致。每次改写都要
落盘，关掉浏览器重开还在。
"""

from __future__ import annotations

from datetime import date

import pytest

from git_daily_report import store
from git_daily_report.models import Commit, DayReport, WorkPoint
from git_daily_report.web.tools import ReportSession, ToolError

DAY = "2026-08-10"


def commit(sha: str, title: str, project: str = "g/应急平台") -> Commit:
    return Commit(project=project, sha=sha, day=DAY, title=title, message=title, web_url="")


def point(point_id: str, title: str, entries: tuple[str, ...], commits: tuple[Commit, ...] = ()):
    return WorkPoint(
        id=point_id,
        day=DAY,
        title=title,
        entries=entries,
        confidence="exact",
        item_number=point_id.split("/")[-1],
        commits=commits,
        generated_title=title,
        generated_entries=entries,
    )


@pytest.fixture
def session(tmp_path):
    days = [
        DayReport(
            day=DAY,
            points=(
                point(f"{DAY}/A", "态势图层重构", ("完成图层切换", "修复图例错位"),
                      (commit("s1", "feat: 完成图层切换"), commit("s2", "fix: 修复图例错位"))),
                point(f"{DAY}/B", "组件库维护", ("补充文档",), (commit("s3", "docs: 补充文档"),)),
            ),
        )
    ]
    store.save_days(
        store.report_path(tmp_path, date(2026, 8, 1), date(2026, 8, 31)),
        date(2026, 8, 1),
        date(2026, 8, 31),
        days,
    )
    return ReportSession(tmp_path, tmp_path, date(2026, 8, 1), date(2026, 8, 31))


def test_set_point_marks_the_text_as_edited_and_persists(session):
    session.set_point(f"{DAY}/A", title="态势图层重构收尾", entries=["图层切换与图例一起改完"])

    reloaded = store.load_days(session.path)[0].points[0]
    assert reloaded.title == "态势图层重构收尾"
    assert reloaded.entries == ("图层切换与图例一起改完",)
    assert reloaded.hand_edited


def test_set_point_can_fill_hours_alone(session):
    session.set_point(f"{DAY}/A", hours=3.5)
    updated = session.find_point(f"{DAY}/A")

    assert updated.hours == 3.5
    assert updated.title == "态势图层重构"
    assert not updated.hand_edited


def test_unknown_point_is_a_clear_error_not_a_crash(session):
    with pytest.raises(ToolError, match="找不到工作点"):
        session.set_point(f"{DAY}/ZZZ", title="x")


def test_move_commits_reassigns_material_and_rewrites_both_sides(session):
    session.move_commits(f"{DAY}/A", ["s2"], to_point_id=f"{DAY}/B")

    source = session.find_point(f"{DAY}/A")
    target = session.find_point(f"{DAY}/B")

    assert [c.sha for c in source.commits] == ["s1"]
    assert source.entries == ("完成图层切换",)
    assert [c.sha for c in target.commits] == ["s3", "s2"]
    assert target.entries == ("补充文档", "修复图例错位")


def test_move_commits_into_a_brand_new_point(session):
    session.move_commits(f"{DAY}/A", ["s2"], to_new_title="图例专项")

    created = next(p for p in session.day(DAY).points if p.title == "图例专项")
    assert [c.sha for c in created.commits] == ["s2"]
    assert created.entries == ("修复图例错位",)


def test_move_commits_leaves_hand_written_text_alone(session):
    session.set_point(f"{DAY}/A", entries=["我自己写的一句话"])
    session.move_commits(f"{DAY}/A", ["s2"], to_point_id=f"{DAY}/B")

    assert session.find_point(f"{DAY}/A").entries == ("我自己写的一句话",)
    assert [c.sha for c in session.find_point(f"{DAY}/A").commits] == ["s1"]


def test_moving_every_commit_out_removes_the_empty_point(session):
    session.move_commits(f"{DAY}/B", ["s3"], to_point_id=f"{DAY}/A")

    assert [p.id for p in session.day(DAY).points] == [f"{DAY}/A"]


def test_move_requires_a_destination(session):
    with pytest.raises(ToolError, match="目标"):
        session.move_commits(f"{DAY}/A", ["s1"])


def test_unknown_sha_is_reported(session):
    with pytest.raises(ToolError, match="s9"):
        session.move_commits(f"{DAY}/A", ["s9"], to_point_id=f"{DAY}/B")


def test_merge_points_folds_entries_and_commits_into_the_first(session):
    session.merge_points([f"{DAY}/A", f"{DAY}/B"], title="应急平台整体推进")

    points = session.day(DAY).points
    assert len(points) == 1
    assert points[0].title == "应急平台整体推进"
    assert points[0].entries == ("完成图层切换", "修复图例错位", "补充文档")
    assert [c.sha for c in points[0].commits] == ["s1", "s2", "s3"]


def test_merge_needs_at_least_two_points(session):
    with pytest.raises(ToolError, match="至少"):
        session.merge_points([f"{DAY}/A"], title="x")


def test_drop_point_removes_it_and_persists(session):
    session.drop_point(f"{DAY}/B")

    assert [p.id for p in session.day(DAY).points] == [f"{DAY}/A"]
    assert len(store.load_days(session.path)[0].points) == 1


def test_dropping_the_last_point_leaves_no_empty_day(session):
    session.drop_point(f"{DAY}/A")
    session.drop_point(f"{DAY}/B")

    assert session.days == []


def test_views_render_from_the_current_state(session):
    session.set_point(f"{DAY}/A", title="改过的标题")

    assert "### 改过的标题" in session.markdown()
    assert "具体任务：" in session.memo()


def test_read_report_is_a_compact_shape_for_the_model(session):
    payload = session.read_report()

    assert payload["range"] == {"since": "2026-08-01", "until": "2026-08-31"}
    first = payload["days"][0]["points"][0]
    assert first["id"] == f"{DAY}/A"
    assert first["commits"] == [
        {"sha": "s1", "title": "feat: 完成图层切换", "project": "g/应急平台"},
        {"sha": "s2", "title": "fix: 修复图例错位", "project": "g/应急平台"},
    ]
