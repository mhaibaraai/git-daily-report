"""三段式文本：段名固定、空项写「无」、整周导出。"""

from __future__ import annotations

from git_daily_report.models import Order, Progress, ScheduleDay
from git_daily_report.render.memo import build_memo, to_text


def test_sections_are_one_line_each_joined_by_fullwidth_semicolon():
    memo = build_memo({"task": ["完成筛选", "补充校验"], "data": ["新增 2 项"], "problem": ["修复坐标偏移"]})

    assert memo == "具体任务：完成筛选；补充校验\n关键数据：新增 2 项\n问题解决过程：修复坐标偏移"


def test_missing_or_blank_sections_are_written_as_wu():
    memo = build_memo({"task": ["完成筛选"], "problem": ["  "]})

    assert memo == "具体任务：完成筛选\n关键数据：无\n问题解决过程：无"


def test_full_text_groups_by_day_and_order():
    progress = Progress(memo="具体任务：完成筛选\n关键数据：无\n问题解决过程：无", issue="无", plan="联调", source="ai")
    days = [
        ScheduleDay(
            "2026-10-26",
            (
                Order("1", "订单列表筛选优化", "任务", "项目", progress),
                Order("2", "报表数据梳理", "任务", "项目", None),
            ),
        )
    ]
    text = to_text(days)

    assert text == (
        "## 2026-10-26\n"
        "### 订单列表筛选优化\n"
        "具体任务：完成筛选\n关键数据：无\n问题解决过程：无\n"
        "存在问题：无\n"
        "明日计划：联调\n"
        "### 报表数据梳理\n"
        "（未生成）\n"
    )


def test_empty_week_exports_nothing():
    assert to_text([]) == ""
