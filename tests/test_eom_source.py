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
    report = {"id": 9, "memo": "具体任务：完成筛选", "ewdProblem": "无", "tomorrowPlan": "联调"}
    stub([{"time": "2026-10-26", "orders": [order(1, "甲", report)]}])
    progress = EomClient(CFG).schedule(date(2026, 10, 26), date(2026, 10, 26))[0].orders[0].progress

    assert progress.source == "eom"
    assert progress.memo == "具体任务：完成筛选"
    assert progress.issue == "无"
    assert progress.plan == "联调"


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
def test_business_error_surfaces_the_server_message():
    responses.add(responses.POST, URL, json={"code": 500, "msg": "服务繁忙"})
    with pytest.raises(EomError, match="服务繁忙"):
        EomClient(CFG).schedule(date(2026, 10, 26), date(2026, 10, 30))
