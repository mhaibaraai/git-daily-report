"""AI 生成：素材组装、结果解析、不合格重试与已填跳过。"""

from __future__ import annotations

import json

from git_daily_report.generate import apply_blocks, build_material, generate, parse_items
from git_daily_report.models import Commit, Order, Progress, ScheduleDay


def commit(day: str, title: str, project: str = "g/order-web") -> Commit:
    return Commit(project, f"{day}-{title}", day, title, title, "")


def order(order_id: str, title: str = "订单列表筛选优化", progress: Progress | None = None) -> Order:
    return Order(order_id, title, "数据治理及开发任务", "示例项目", progress)


def item(day: str, order_id: str, **overrides) -> dict:
    base = {
        "day": day,
        "orderId": order_id,
        "task": ["完成订单列表筛选"],
        "data": ["新增 2 项筛选条件"],
        "problem": [],
        "issue": "",
        "plan": "完成筛选结果联调",
        "rate": 50,
        "hours": 3,
    }
    return {**base, **overrides}


class FakeAi:
    """按顺序回放预设的回复，并记下每次收到的消息；回复按两段流式推给回调。"""

    def __init__(self, *replies: dict, thinking: str = "") -> None:
        self._replies = list(replies)
        self._thinking = thinking
        self.calls: list[list[dict]] = []

    def __call__(self, messages: list[dict], on_chunk) -> str:
        self.calls.append(messages)
        if self._thinking:
            on_chunk("thinking", self._thinking)
        text = json.dumps(self._replies.pop(0), ensure_ascii=False)
        on_chunk("content", text[: len(text) // 2])
        on_chunk("content", text)
        return text


def test_material_lists_pending_blocks_and_cleaned_commits():
    days = [ScheduleDay("2026-10-26", (order("1"),))]
    commits = [
        commit("2026-10-27", "feat: 新增订单列表筛选"),
        commit("2026-10-27", "Merge branch 'dev' into 'main'"),
    ]
    material = build_material(days, commits)

    assert material["schedule"][0]["orders"][0]["id"] == "1"
    assert material["schedule"][0]["orders"][0]["工单标题"] == "订单列表筛选优化"
    assert material["commits"] == [
        {"day": "2026-10-27", "project": "g/order-web", "title": "新增订单列表筛选"}
    ]


def test_every_pending_block_gets_a_three_section_memo():
    days = [ScheduleDay("2026-10-26", (order("1"),)), ScheduleDay("2026-10-27", (order("1"),))]
    ai = FakeAi({"items": [item("2026-10-26", "1"), item("2026-10-27", "1")]})
    result = generate(days, [commit("2026-10-27", "新增筛选")], ai)

    progress = result[0].orders[0].progress
    assert progress.source == "ai"
    assert progress.memo == "具体任务：完成订单列表筛选\n关键数据：新增 2 项筛选条件\n问题解决过程：无"
    assert progress.issue == "无"
    assert progress.plan == "完成筛选结果联调"
    assert progress.problems == ()
    assert len(ai.calls) == 1


def test_filled_blocks_are_neither_sent_nor_overwritten():
    filled = Progress(memo="具体任务：已填", issue="无", plan="联调", source="eom", rate=40, hours=3)
    days = [ScheduleDay("2026-10-26", (order("1", progress=filled), order("2", "报表数据梳理")))]
    ai = FakeAi({"items": [item("2026-10-26", "2")]})
    result = generate(days, [], ai)

    sent = json.loads(ai.calls[0][-1]["content"])
    pending_ids = [o["id"] for d in sent["schedule"] for o in d["orders"] if not o.get("已填写")]
    assert pending_ids == ["2"]
    assert result[0].orders[0].progress is filled
    assert result[0].orders[1].progress.source == "ai"


def test_failing_blocks_are_retried_once_with_the_reasons():
    days = [ScheduleDay("2026-10-26", (order("1"), order("2", "报表数据梳理")))]
    ai = FakeAi(
        {"items": [item("2026-10-26", "1", task=["修复 getUserInfo 报错"]), item("2026-10-26", "2")]},
        {"items": [item("2026-10-26", "1", task=["修复用户信息读取报错"])]},
    )
    result = generate(days, [], ai)

    assert len(ai.calls) == 2
    retry = ai.calls[1][-1]["content"]
    assert "getUserInfo" in retry
    assert result[0].orders[0].progress.memo.startswith("具体任务：修复用户信息读取报错")
    assert result[0].orders[0].progress.problems == ()


def test_still_failing_after_retry_is_flagged_not_dropped():
    days = [ScheduleDay("2026-10-26", (order("1"),))]
    bad = {"items": [item("2026-10-26", "1", task=["修复 getUserInfo 报错"])]}
    result = generate(days, [], FakeAi(bad, bad))

    progress = result[0].orders[0].progress
    assert progress.memo.startswith("具体任务：修复 getUserInfo 报错")
    assert any("getUserInfo" in p for p in progress.problems)


def test_missing_block_is_retried_then_flagged():
    days = [ScheduleDay("2026-10-26", (order("1"),))]
    result = generate(days, [], FakeAi({"items": []}, {"items": []}))

    progress = result[0].orders[0].progress
    assert progress.memo == ""
    assert progress.problems == ("AI 没有生成这一块",)


def test_malformed_reply_counts_as_missing_not_a_crash():
    days = [ScheduleDay("2026-10-26", (order("1"),))]

    def broken(_messages, _on_chunk):
        return "不是 JSON"

    progress = generate(days, [], broken)[0].orders[0].progress
    assert progress.problems == ("AI 没有生成这一块",)


def test_nothing_pending_skips_the_ai_call():
    filled = Progress(memo="具体任务：已填", issue="无", plan="联调", source="eom")
    ai = FakeAi()
    generate([ScheduleDay("2026-10-26", (order("1", progress=filled),))], [], ai)

    assert ai.calls == []


def test_progress_reports_thinking_blocks_written_and_validation():
    days = [ScheduleDay("2026-10-26", (order("1"), order("2", "报表数据梳理")))]
    ai = FakeAi({"items": [item("2026-10-26", "1"), item("2026-10-26", "2")]}, thinking="想" * 700)
    lines: list[str] = []
    generate(days, [], ai, lines.append)

    assert lines[0] == "AI 生成：等待响应，共 2 块"
    assert "AI 生成：思考中，已输出 700 字" in lines
    assert "AI 生成：已写出 2/2 块" in lines
    assert lines[-1] == "校验：全部通过"


def test_progress_announces_the_retry():
    days = [ScheduleDay("2026-10-26", (order("1"),))]
    bad = {"items": [item("2026-10-26", "1", task=["修复 getUserInfo 报错"])]}
    lines: list[str] = []
    generate(days, [], FakeAi(bad, bad), lines.append)

    assert "校验：1 块不合格，交给 AI 重写" in lines
    assert "AI 重写：已写出 1/1 块" in lines
    assert lines[-1] == "校验：1 块仍需复核"


def test_parse_items_indexes_by_day_and_order():
    raw = json.dumps({"items": [item("2026-10-26", "1"), "垃圾", {"day": "2026-10-27", "orderId": 2}]})

    assert set(parse_items(raw)) == {("2026-10-26", "1"), ("2026-10-27", "2")}


def test_parse_items_tolerates_malformed_replies():
    assert parse_items("不是 JSON") == {}
    assert parse_items("[]") == {}
    assert parse_items('{"items": "x"}') == {}


def test_apply_blocks_fills_pending_and_reports_failures():
    days = [ScheduleDay("2026-10-26", (order("1"), order("2")))]
    blocks = parse_items(json.dumps({"items": [item("2026-10-26", "1", task=["改 index.vue"])]}))

    result, failures = apply_blocks(days, blocks)

    assert set(failures) == {("2026-10-26", "1"), ("2026-10-26", "2")}
    assert any("代码化" in p for p in failures[("2026-10-26", "1")])
    assert result[0].orders[0].progress.problems
    assert result[0].orders[1].progress is None


def test_apply_blocks_only_overwrites_given_keys_and_never_eom():
    eom = Progress(memo="具体任务：已填", issue="无", plan="联调", source="eom", rate=40, hours=1.5)
    days = [ScheduleDay("2026-10-26", (order("1"), order("2"), order("3", progress=eom)))]
    first, _ = apply_blocks(days, parse_items(json.dumps({"items": [
        item("2026-10-26", "1"), item("2026-10-26", "2", plan=""),
    ]})))

    retry = {("2026-10-26", "2"): item("2026-10-26", "2", plan="完成验收"),
             ("2026-10-26", "3"): item("2026-10-26", "3")}
    result, failures = apply_blocks(first, retry)

    assert failures == {}
    assert result[0].orders[0].progress == first[0].orders[0].progress
    assert result[0].orders[1].progress.plan == "完成验收"
    assert result[0].orders[2].progress is eom


def test_daily_overflow_is_retried_with_preserved_context():
    filled = Progress("具体任务：已填", "无", "核对", "eom", rate=10, hours=4.5)
    days = [ScheduleDay("2026-10-26", (order("1", progress=filled), order("2")))]
    ai = FakeAi({"items": [item("2026-10-26", "2", hours=4)]},
                {"items": [item("2026-10-26", "2", hours=3)]})
    result = generate(days, [], ai)
    assert len(ai.calls) == 2
    retry = json.loads(ai.calls[1][-1]["content"])
    assert retry["schedule"][0]["orders"][0]["hours"] == 4.5
    assert "7.5" in retry["只重写这些"][0]["上次的问题"][0]
    assert result[0].orders[1].progress.hours == 3
    assert result[0].orders[1].progress.problems == ()


def test_partial_apply_revalidates_old_blocks_when_daily_total_changes():
    days = [ScheduleDay("2026-10-26", (order("1"), order("2")))]
    first, _ = apply_blocks(days, {("2026-10-26", "1"): item("2026-10-26", "1", hours=4.5)})
    result, failures = apply_blocks(first, {("2026-10-26", "2"): item("2026-10-26", "2", hours=4)})
    assert len(failures) == 2
    assert all(any("7.5" in p for p in o.progress.problems) for o in result[0].orders)


def test_multiline_task_cannot_hide_code_terms_after_memo_conversion():
    days = [ScheduleDay("2026-10-26", (order("1"),))]
    result, failures = apply_blocks(days, {
        ("2026-10-26", "1"): item("2026-10-26", "1", task=["完成核对\ngetUserInfo"]),
    })
    assert any("getUserInfo" in p for p in failures[("2026-10-26", "1")])
    assert result[0].orders[0].progress.problems


def test_material_preserves_previous_allocation_context_without_changing_order_id():
    from dataclasses import replace
    linked = replace(order("new-id"), rate_min=70, previous_day="2026-10-20",
                     previous_memo="具体任务：完成首轮验证\n关键数据：无\n问题解决过程：无")
    entry = build_material([ScheduleDay("2026-10-21", (linked,))], [])["schedule"][0]["orders"][0]
    assert entry["id"] == "new-id"
    assert entry["rateMin"] == 70
    assert entry["previousDay"] == "2026-10-20"
    assert "完成首轮验证" in entry["previousMemo"]
    assert Order.from_dict(linked.to_dict()) == linked
