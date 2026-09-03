"""Markdown 与三段式两种产出视图。"""

from __future__ import annotations

from git_daily_report.models import Commit, DayReport, WorkPoint
from git_daily_report.render.markdown import to_markdown
from git_daily_report.render.memo import build_memo, to_memo


def point(title, entries, confidence="exact", candidates=(), commits=(), point_id="2026-08-10/A"):
    return WorkPoint(
        id=point_id,
        day="2026-08-10",
        title=title,
        entries=entries,
        confidence=confidence,
        item_number="A",
        candidates=candidates,
        commits=commits,
    )


DAYS = [
    DayReport(
        day="2026-08-10",
        points=(
            point("态势图层重构", ("完成图层切换逻辑", "修复图例渲染错位")),
            point("组件库维护", ("补充文档",), point_id="2026-08-10/B"),
        ),
    )
]


def test_markdown_is_date_point_entry_three_levels():
    text = to_markdown(DAYS)
    assert text.splitlines()[:6] == [
        "## 2026-08-10",
        "### 态势图层重构",
        "- 完成图层切换逻辑",
        "- 修复图例渲染错位",
        "### 组件库维护",
        "- 补充文档",
    ]


def test_markdown_stays_clean_by_default_for_pasting():
    days = [DayReport(day="2026-08-10", points=(point("任务", ("甲",), confidence="unmatched"),))]
    assert "⚠" not in to_markdown(days)


def test_annotated_markdown_flags_what_needs_review():
    days = [
        DayReport(
            day="2026-08-10",
            points=(
                point("任务甲", ("甲",), confidence="ambiguous", candidates=("A", "B")),
                point("g/项目丙", ("乙",), confidence="unmatched", point_id="2026-08-10/~x"),
                point("任务丁", ("丙",), confidence="fuzzy", point_id="2026-08-10/C"),
            ),
        )
    ]
    text = to_markdown(days, annotate=True)

    assert "### 任务甲 ⚠ 归属存疑：A / B" in text
    assert "### g/项目丙 ⚠ 未关联" in text
    assert "### 任务丁 ⚠ 按标题推断" in text


def test_memo_view_splits_three_sections_by_keyword():
    commits = (
        Commit("g/应急平台", "s1", "2026-08-10", "a", "a", ""),
        Commit("g/应急平台", "s2", "2026-08-10", "b", "b", ""),
    )
    days = [
        DayReport(
            day="2026-08-10",
            points=(point("态势图层", ("完成图层切换", "修复图例错位"), commits=commits),),
        )
    ]
    text = to_memo(days)

    assert "具体任务：完成图层切换" in text
    assert "问题解决过程：修复图例错位" in text
    assert "关键数据：g/应急平台 提交 2 次" in text


def test_memo_leaves_sections_empty_rather_than_inventing():
    days = [DayReport(day="2026-08-10", points=(point("任务", ("完成配置项梳理",)),))]
    text = to_memo(days)

    assert "具体任务：完成配置项梳理" in text
    assert "关键数据：" in text
    assert "问题解决过程：" in text


def test_build_memo_uses_standard_labels():
    memo = build_memo({"task": ["A", "B"], "data": ["C"], "problem": ["D"]})
    assert memo == "具体任务：A；B\n关键数据：C\n问题解决过程：D"


def test_build_memo_keeps_empty_sections_as_headings():
    """段标题必须始终在场，前端靠它识别；缺内容是缺内容，不是缺段。"""
    memo = build_memo({"task": ["A"], "data": [], "problem": []})
    assert memo.splitlines() == ["具体任务：A", "关键数据：", "问题解决过程："]
