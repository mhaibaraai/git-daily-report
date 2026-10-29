"""EOM 日程表接口：请求形状、跨月、已填内容与鉴权失败。"""

from __future__ import annotations

import json
from datetime import date

import pytest
import responses

from git_daily_report.config import EomConfig
from git_daily_report.sources.eom import EomAuthError, EomClient, EomError

CFG = EomConfig(base_url="http://eom.test/api", token="TOKEN")
URL = "http://eom.test/api/core/busi/exec"


def order(order_id: int, title: str, report: dict | None = None) -> dict:
    return {
        "id": order_id,
        "ewlTitle": title,
        "eTaskTitle": "数据治理及开发任务",
        "eProjectName": "示例项目",
        "report": report,
    }


def stub(days: list[dict], status: int = 200, code: int = 200) -> None:
    responses.add(responses.POST, URL, json={"code": code, "datas": {"2.1": days}}, status=status)


@responses.activate
def test_request_carries_token_headers_and_schedule_condition():
    stub([])
    EomClient(CFG).schedule(date(2026, 10, 26), date(2026, 10, 30))

    call = responses.calls[0].request
    assert call.headers["token"] == "TOKEN"
    assert call.headers["application"] == "platform"
    assert call.headers["tenant"] == "default"
    body = json.loads(call.body)
    assert body["menuId"] == "ScheduleP"
    assert body["condition"] == {"op": "schedule", "year": 2026, "month": 10}


@responses.activate
def test_days_are_filtered_to_the_range_and_empty_days_dropped():
    stub(
        [
            {"time": "2026-10-23", "orders": [order(1, "订单列表筛选优化")]},
            {"time": "2026-10-26", "orders": [order(1, "订单列表筛选优化")]},
            {"time": "2026-10-27", "orders": []},
        ]
    )
    days = EomClient(CFG).schedule(date(2026, 10, 26), date(2026, 10, 30))

    assert [d.day for d in days] == ["2026-10-26"]
    first = days[0].orders[0]
    assert first.id == "1"
    assert first.title == "订单列表筛选优化"
    assert first.project_name == "示例项目"
    assert first.progress is None


@responses.activate
def test_range_across_months_queries_both_months():
    stub([{"time": "2026-09-30", "orders": [order(1, "甲")]}])
    stub([{"time": "2026-10-01", "orders": [order(2, "乙")]}])
    days = EomClient(CFG).schedule(date(2026, 9, 28), date(2026, 10, 4))

    months = [json.loads(c.request.body)["condition"]["month"] for c in responses.calls]
    assert months == [9, 10]
    assert [d.day for d in days] == ["2026-09-30", "2026-10-01"]


@responses.activate
def test_already_filled_report_is_kept_as_eom_progress():
    report = {"id": 9, "memo": "具体任务：完成筛选", "ewdProblem": "无", "tomorrowPlan": "联调",
              "ewdRate": 60, "ewdWorkRealTimes": 3}
    stub([{"time": "2026-10-26", "orders": [order(1, "甲", report)]}])
    progress = EomClient(CFG).schedule(date(2026, 10, 26), date(2026, 10, 26))[0].orders[0].progress

    assert progress.source == "eom"
    assert progress.memo == "具体任务：完成筛选"
    assert progress.issue == "无"
    assert progress.plan == "联调"
    assert progress.rate == 60
    assert progress.hours == 3


@responses.activate
def test_days_after_today_are_not_returned():
    stub(
        [
            {"time": "2026-10-28", "orders": [order(1, "甲")]},
            {"time": "2026-10-29", "orders": [order(1, "甲")]},
        ]
    )
    days = EomClient(CFG).schedule(date(2026, 10, 26), date(2026, 11, 1), today=date(2026, 10, 28))

    assert [d.day for d in days] == ["2026-10-28"]


@responses.activate
@pytest.mark.parametrize(("status", "code"), [(401, 401), (200, 401)])
def test_expired_token_asks_to_refresh(status, code):
    stub([], status=status, code=code)
    with pytest.raises(EomAuthError, match="EOM_TOKEN"):
        EomClient(CFG).schedule(date(2026, 10, 26), date(2026, 10, 30))


@responses.activate
def test_business_error_is_reported_without_echoing_the_server_message():
    responses.add(responses.POST, URL, json={"code": 500, "msg": "服务繁忙"})
    with pytest.raises(EomError, match="返回错误"):
        EomClient(CFG).schedule(date(2026, 10, 26), date(2026, 10, 30))


@responses.activate
def test_schedule_errors_do_not_expose_server_supplied_credentials():
    responses.add(responses.POST, URL, json={"code": 500, "msg": "token=EXPOSED"})
    with pytest.raises(EomError) as failure:
        EomClient(CFG).schedule(date(2026, 10, 26), date(2026, 10, 30))
    assert "EXPOSED" not in str(failure.value)


@responses.activate
def test_schedule_keeps_only_selected_days_and_adds_trusted_rate_bounds():
    stub([
        {"time": "2026-10-18", "orders": [order(1, "甲", {"id": 9, "ewdRate": 50})]},
        {"time": "2026-10-19", "orders": [order(1, "甲")]},
        {"time": "2026-10-20", "orders": [order(2, "乙", {"id": 9, "ewdRate": 10})]},
        {"time": "2026-10-21", "orders": [order(1, "甲", {"id": 9, "ewdRate": 70})]},
        {"time": "2026-10-22", "orders": [order(1, "甲", {"id": 9, "ewdRate": "60"})]},
        {"time": "2026-10-30", "orders": [order(1, "甲", {"id": 9, "ewdRate": 30})]},
    ])
    days = EomClient(CFG).schedule(date(2026, 10, 19), date(2026, 10, 19), today=date(2026, 10, 29))
    assert [day.day for day in days] == ["2026-10-19"]
    assert days[0].orders[0].rate_min == 50
    assert days[0].orders[0].rate_max == 70
    assert len(responses.calls) == 1


def scheduled(order_id, start, end, rate=None, **overrides):
    value = {
        **order(order_id, "订单治理"), "eProjectId": "项目一", "eTaskId": "任务一",
        "ewlWorker": "人员一", "type": 1, "ewlStartTime": start,
        "ewlEndTime": end, "ewlNum": f"工单{order_id}",
    }
    if rate is not None:
        value["report"] = {"id": 9, "ewdRate": rate, "memo": "具体任务：完成接口联调"}
    return {**value, **overrides}


@responses.activate
def test_same_title_adjacent_orders_have_independent_progress():
    stub([
        {"time": "2026-10-19", "orders": [scheduled(1, "2026-10-12", "2026-10-20", 65)]},
        {"time": "2026-10-21", "orders": [scheduled(2, "2026-10-21", "2026-10-30")]},
    ])
    days = EomClient(CFG).schedule(date(2026, 10, 21), date(2026, 10, 28), today=date(2026, 10, 29))
    current = days[0].orders[0]
    assert current.id == "2"
    assert current.rate_min is None
    assert current.previous_day == ""
    assert current.previous_memo == ""
    assert current.continuity_id == "2"
    assert len(responses.calls) == 1


@responses.activate
@pytest.mark.parametrize("change", [
    {"eProjectId": "项目二"}, {"eTaskId": "任务二"}, {"ewlWorker": "人员二"},
    {"ewlTitle": "其他治理"}, {"type": 2}, {"ewlWorker": None},
])
def test_same_title_does_not_join_different_or_incomplete_business_identity(change):
    stub([
        {"time": "2026-10-19", "orders": [scheduled(1, "2026-10-12", "2026-10-20", 65)]},
        {"time": "2026-10-22", "orders": [scheduled(2, "2026-10-21", "2026-10-30", **change)]},
    ])
    current = EomClient(CFG).schedule(date(2026, 10, 22), date(2026, 10, 22), today=date(2026, 10, 29))[0].orders[0]
    assert current.rate_min is None
    assert current.previous_memo == ""


@responses.activate
@pytest.mark.parametrize("previous_end,previous_rate", [("2026-10-22", 65), ("2026-10-20", 100)])
def test_overlapping_orders_or_completed_reopen_do_not_share_progress(previous_end, previous_rate):
    stub([
        {"time": "2026-10-19", "orders": [scheduled(1, "2026-10-12", previous_end, previous_rate)]},
        {"time": "2026-10-22", "orders": [scheduled(2, "2026-10-21", "2026-10-30")]},
    ])
    current = EomClient(CFG).schedule(date(2026, 10, 22), date(2026, 10, 22), today=date(2026, 10, 29))[0].orders[0]
    assert current.rate_min is None
    assert current.previous_memo == ""


@responses.activate
def test_parallel_predecessors_are_ambiguous_and_do_not_join_current_order():
    stub([
        {"time": "2026-10-19", "orders": [scheduled(1, "2026-10-12", "2026-10-20", 65),
                                            scheduled(3, "2026-10-12", "2026-10-20", 70)]},
        {"time": "2026-10-22", "orders": [scheduled(2, "2026-10-21", "2026-10-30")]},
    ])
    current = EomClient(CFG).schedule(date(2026, 10, 22), date(2026, 10, 22), today=date(2026, 10, 29))[0].orders[0]
    assert current.rate_min is None
    assert current.previous_memo == ""


@responses.activate
def test_new_order_does_not_read_previous_same_title_order_months():
    stub([{"time": "2026-10-01", "orders": [scheduled(2, "2026-10-01", "2026-10-30")]}])
    current = EomClient(CFG).schedule(date(2026, 10, 1), date(2026, 10, 1), today=date(2026, 10, 29))[0].orders[0]
    assert current.id == "2" and current.rate_min is None
    assert current.previous_day == ""
    months = [json.loads(call.request.body)["condition"]["month"] for call in responses.calls]
    assert months == [10]


@responses.activate
def test_unfilled_adjacent_orders_keep_independent_identity():
    stub([
        {"time": "2026-10-19", "orders": [scheduled(1, "2026-10-12", "2026-10-20")]},
        {"time": "2026-10-21", "orders": [scheduled(2, "2026-10-21", "2026-10-30")]},
    ])
    days = EomClient(CFG).schedule(date(2026, 10, 19), date(2026, 10, 28), today=date(2026, 10, 29))
    assert [day.orders[0].id for day in days] == ["1", "2"]
    assert [day.orders[0].continuity_id for day in days] == ["1", "2"]


@responses.activate
def test_completed_predecessor_splits_continuity_chain():
    stub([
        {"time": "2026-10-19", "orders": [scheduled(1, "2026-10-12", "2026-10-20", 100)]},
        {"time": "2026-10-21", "orders": [scheduled(2, "2026-10-21", "2026-10-30")]},
    ])
    days = EomClient(CFG).schedule(date(2026, 10, 19), date(2026, 10, 28), today=date(2026, 10, 29))
    assert [day.orders[0].continuity_id for day in days] == ["1", "2"]


@responses.activate
def test_same_order_keeps_its_own_cross_month_history():
    stub([{"time": "2026-10-01", "orders": [scheduled(2, "2026-09-01", "2026-10-30")]}])
    stub([{"time": "2026-09-30", "orders": [scheduled(2, "2026-09-01", "2026-10-30", 65)]}])
    current = EomClient(CFG).schedule(date(2026, 10, 1), date(2026, 10, 1), today=date(2026, 10, 29))[0].orders[0]
    assert current.rate_min == 65
    assert current.previous_day == "2026-09-30"
    assert current.previous_memo == "具体任务：完成接口联调"
    assert len(responses.calls) == 2


@responses.activate
def test_continuity_returns_days_for_each_order_id_separately():
    stub([
        {"time": "2026-10-12", "orders": [scheduled(1, "2026-10-12", "2026-10-13", 20)]},
        {"time": "2026-10-13", "orders": [scheduled(1, "2026-10-12", "2026-10-13", 60)]},
        {"time": "2026-10-14", "orders": [scheduled(2, "2026-10-14", "2026-10-15", 10)]},
        {"time": "2026-10-15", "orders": [scheduled(2, "2026-10-14", "2026-10-15", 50)]},
    ])
    _, days = EomClient(CFG).continuity(date(2026, 10, 12), date(2026, 10, 15), today=date(2026, 10, 29))
    assert days == {"1": ["2026-10-12", "2026-10-13"], "2": ["2026-10-14", "2026-10-15"]}


@responses.activate
def test_continuity_reads_only_selected_order_future_months():
    stub([
        {"time": "2026-10-30", "orders": [scheduled(1, "2026-10-30", "2026-11-02")]},
        {"time": "2026-10-31", "orders": [scheduled(1, "2026-10-30", "2026-11-02")]},
    ])
    stub([
        {"time": "2026-11-02", "orders": [scheduled(1, "2026-10-30", "2026-11-02")]},
        {"time": "2026-11-03", "orders": [scheduled(2, "2026-11-03", "2026-12-31")]},
    ])
    bounded, days = EomClient(CFG).continuity(date(2026, 10, 30), date(2026, 10, 30), today=date(2026, 10, 30))
    assert list(bounded) == ["2026-10-30"]
    assert days == {"1": ["2026-10-30", "2026-10-31", "2026-11-02"]}
    months = [json.loads(call.request.body)["condition"]["month"] for call in responses.calls]
    assert months == [10, 11]
