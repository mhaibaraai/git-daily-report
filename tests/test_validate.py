"""生成结果的规则校验：代码化描述、必填项、空项补「无」。"""

from __future__ import annotations

import pytest

from git_daily_report.validate import check_block, code_terms, fill_empty


@pytest.mark.parametrize(
    "text",
    [
        "修改 `config` 配置",
        "调整 index.vue 布局",
        "重构 src/components 目录",
        "新增 getUserInfo 方法",
        "调整 MapLayer 渲染顺序",
        "修复 user_id 为空",
        "合并 feature-map 分支",
        "feat: 新增筛选",
        "调用 init() 初始化",
        "合并 dev 分支",
    ],
)
def test_code_like_wording_is_flagged(text):
    assert code_terms(text)


@pytest.mark.parametrize(
    "text",
    [
        "完成订单状态回写，覆盖 12 个门店",
        "梳理 ERP 库存流程数据",
        "完成 V1.0 版本联调",
        "优化 GIS 图层加载速度，从 3 秒降至 1 秒",
        "无",
    ],
)
def test_business_wording_passes(text):
    assert code_terms(text) == []


def test_empty_sections_become_wu():
    filled = fill_empty({"task": ["完成筛选"], "data": [], "problem": ["  "]})

    assert filled == {"task": ["完成筛选"], "data": ["无"], "problem": ["无"]}


def block(**overrides):
    base = {
        "task": ["完成订单列表筛选"],
        "data": ["无"],
        "problem": ["无"],
        "issue": "无",
        "plan": "完成筛选结果联调",
        "rate": 50,
        "hours": 3,
    }
    return {**base, **overrides}


def test_clean_block_has_no_problems():
    assert check_block(block()) == []


def test_task_must_not_be_empty_or_wu():
    assert any("具体任务" in p for p in check_block(block(task=["无"])))
    assert any("具体任务" in p for p in check_block(block(task=[])))


def test_plan_is_required():
    assert any("明日计划" in p for p in check_block(block(plan="")))


def test_code_terms_are_reported_with_the_offending_word():
    problems = check_block(block(problem=["修复 getUserInfo 报错"]))

    assert any("getUserInfo" in p for p in problems)


def test_amended_rate_ignores_stale_bounds_but_must_not_regress():
    from git_daily_report.models import Order, Progress, ScheduleDay
    from git_daily_report.validate import validate_days

    def day(d, rate):
        p = Progress(memo="随意 text", issue="", plan="", source="amend", rate=rate, hours=3,
                     report_id="r", original_rate=90)
        return ScheduleDay(d, (Order("1", "t", "t", "p", p, rate_min=90),))

    ok = validate_days([day("2026-10-12", 20), day("2026-10-13", 30)])
    assert not any(o.progress.problems for d in ok for o in d.orders)
    bad = validate_days([day("2026-10-12", 30), day("2026-10-13", 20)])
    assert bad[1].orders[0].progress.problems
