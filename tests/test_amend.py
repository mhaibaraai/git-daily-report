"""修正已填日报：每个工单 ID 独立重排累计进展，只改百分比。"""

from __future__ import annotations

from datetime import date

import pytest
import responses

from git_daily_report.config import EomConfig
from git_daily_report.sources.eom import EomClient

CFG = EomConfig(base_url="http://eom.test/api", token="TOKEN")
URL = "http://eom.test/api/core/busi/exec"
TODAY = date(2026, 10, 29)


def scheduled(order_id, start, end, rate=None):
    value = {
        "id": order_id, "ewlTitle": "订单治理", "eTaskTitle": "任务", "eProjectName": "项目",
        "eProjectId": "项目一", "eTaskId": "任务一", "ewlWorker": "人员一", "type": 1,
        "ewlStartTime": start, "ewlEndTime": end, "report": None,
    }
    if rate is not None:
        value["report"] = {"id": f"r{order_id}-{start}-{rate}", "ewdRate": rate, "ewdWorkRealTimes": 3,
                           "memo": "具体任务：完成接口联调", "ewdProblem": "无", "tomorrowPlan": "继续"}
    return value


def stub(days):
    responses.add(responses.POST, URL, json={"code": 200, "datas": {"2.1": days}})


def two_segments():
    first = [("2026-10-12", 10), ("2026-10-13", 40), ("2026-10-14", 64)]
    stub([{"time": day, "orders": [scheduled(1, "2026-10-12", "2026-10-14", rate)]} for day, rate in first]
         + [{"time": day, "orders": [scheduled(2, "2026-10-15", "2026-10-17")]}
            for day in ("2026-10-15", "2026-10-16", "2026-10-17")])


def propose(since, until):
    from git_daily_report.amend import propose as run
    return run(EomClient(CFG), since, until, today=TODAY)


@responses.activate
def test_rates_are_spread_only_across_the_current_order_id():
    two_segments()
    days = propose(date(2026, 10, 12), date(2026, 10, 14))
    rates = [(d.day, d.orders[0].progress.rate) for d in days]
    assert rates == [("2026-10-12", 33), ("2026-10-13", 67), ("2026-10-14", 100)]
    first = days[0].orders[0].progress
    assert first.source == "amend" and first.original_rate == 10
    assert first.report_id == "r1-2026-10-12-10"
    assert first.hours == 3 and first.memo == "具体任务：完成接口联调"
    assert not any(o.progress.problems for d in days for o in d.orders)


@responses.activate
def test_unchanged_rate_stays_as_filled_report():
    stub([{"time": "2026-10-12", "orders": [scheduled(1, "2026-10-12", "2026-10-13", 50)]},
          {"time": "2026-10-13", "orders": [scheduled(1, "2026-10-12", "2026-10-13", 70)]}])
    days = propose(date(2026, 10, 12), date(2026, 10, 13))
    assert [d.orders[0].progress.source for d in days] == ["eom", "amend"]
    assert days[1].orders[0].progress.rate == 100


@responses.activate
def test_partial_chain_range_is_rejected_to_avoid_regressing_earlier_reports():
    two_segments()
    with pytest.raises(ValueError, match="2026-10-12"):
        propose(date(2026, 10, 13), date(2026, 10, 14))


@responses.activate
def test_new_order_id_can_be_amended_without_including_previous_same_title_order():
    stub([
        {"time": "2026-10-14", "orders": [scheduled(1, "2026-10-12", "2026-10-14", 80)]},
        {"time": "2026-10-15", "orders": [scheduled(2, "2026-10-15", "2026-10-17", 20)]},
        {"time": "2026-10-16", "orders": [scheduled(2, "2026-10-15", "2026-10-17", 40)]},
        {"time": "2026-10-17", "orders": [scheduled(2, "2026-10-15", "2026-10-17", 60)]},
    ])
    days = propose(date(2026, 10, 15), date(2026, 10, 17))
    assert [d.orders[0].progress.rate for d in days] == [33, 67, 100]
    assert all(d.orders[0].id == "2" for d in days)
    assert all(not d.orders[0].progress.problems for d in days)
