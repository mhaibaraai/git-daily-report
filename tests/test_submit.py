"""提交日程表：写前预检、单次发送、回读与结果未知恢复。"""

from __future__ import annotations

import json
from datetime import date

import pytest
import requests
import responses

from git_daily_report.config import EomConfig


CFG = EomConfig("https://eom.test", "SECRET")
URL = "https://eom.test/core/busi/exec"
SINCE = date(2026, 10, 19)
UNTIL = date(2026, 10, 20)


def raw_order(order_id="1", report=None):
    return {
        "id": order_id, "ewlTitle": "订单核验", "ewlWorkPlanTimes": 52.5,
        "ewlWorker": "123", "ewlWorkerName": "示例用户", "ewlDept": "456",
        "ewlNum": "GD001", "ewlStartTime": "2026-10-01 09:00:00",
        "ewlEndTime": "2026-10-30 17:45:00", "eProjectId": "789",
        "eTaskId": "987", "eProjectName": "业务项目", "eTaskTitle": "业务任务",
        "type": 1, "report": report,
    }


def report(hours=7.5, rate=65, memo="具体任务：完成订单核验\n关键数据：无\n问题解决过程：无"):
    return {
        "id": "900", "memo": memo, "ewdProblem": "无", "tomorrowPlan": "继续核验",
        "ewdWorkRealTimes": hours, "ewdRate": rate,
        "ewdDate": "2026-10-19", "ewlOrderId": "1",
    }


def archive(tmp_path, days=None):
    path = tmp_path / "report.json"
    if days is None:
        days = [{"day": "2026-10-19", "orders": [local_order()]}]
    path.write_text(json.dumps({"schemaVersion": 3, "days": days}), encoding="utf-8")
    return path


def local_order(order_id="1", hours=7.5, rate=65):
    return {
        "id": order_id, "title": "订单核验", "taskTitle": "业务任务", "projectName": "业务项目",
        "progress": {"source": "ai", "memo": report()["memo"], "issue": "无",
                     "plan": "继续核验", "hours": hours, "rate": rate, "problems": []},
    }


def schedule(orders=None, day="2026-10-19", more=None):
    days = [{"time": day, "orders": orders or [raw_order()]}]
    days.extend(more or [])
    responses.add(responses.POST, URL, json={"code": 200, "datas": {"2.1": days}})


def run(path, **kwargs):
    from git_daily_report.submit import submit_report
    return submit_report(CFG, path, SINCE, UNTIL, **kwargs)


@responses.activate
def test_submit_uses_live_metadata_and_confirms_the_saved_values(tmp_path):
    path = archive(tmp_path)
    schedule()
    responses.add(responses.POST, URL, json={"code": 200})
    schedule([raw_order(report=report())])
    result = run(path)
    assert result["days"][0]["status"] == "submitted"
    body = json.loads(responses.calls[1].request.body)
    assert body["buttonId"] == "increase"
    row = body["condition"]["rows"][0]
    assert body["condition"]["op"] == "commitReport"
    assert row["ewlOrderId"] == "1" and row["ewdWorker"] == "123"
    assert row["ewdWorkRealTimes"] == 7.5 and row["ewdRate"] == 65
    assert row["status"] == 1 and row["op"] == 4
    assert row["ewdDate"] == "2026-10-19"
    from git_daily_report.store import load_days
    assert load_days(path)[0].orders[0].progress.source == "eom"


@responses.activate
def test_dry_run_does_not_write_or_change_archive(tmp_path):
    path = archive(tmp_path)
    before = path.read_bytes()
    schedule()
    result = run(path, dry_run=True)
    assert result["days"][0]["status"] == "ready"
    assert len(responses.calls) == 1 and path.read_bytes() == before


@responses.activate
def test_existing_hours_count_toward_the_day_limit(tmp_path):
    path = archive(tmp_path)
    schedule([raw_order(), raw_order("2", report(hours=1))])
    result = run(path)
    assert result["days"][0]["status"] == "blocked"
    assert len(responses.calls) == 1


@responses.activate
def test_new_unfilled_schedule_order_blocks_the_day(tmp_path):
    path = archive(tmp_path)
    schedule([raw_order(), raw_order("2")])
    assert run(path)["days"][0]["status"] == "blocked"
    assert len(responses.calls) == 1


@responses.activate
def test_missing_live_metadata_blocks_instead_of_guessing(tmp_path):
    path = archive(tmp_path)
    raw = raw_order()
    del raw["ewlWorker"]
    schedule([raw])
    assert run(path)["days"][0]["status"] == "blocked"
    assert len(responses.calls) == 1


@responses.activate
def test_timeout_never_retries_and_requires_explicit_retry_after_restart(tmp_path):
    path = archive(tmp_path)
    schedule()
    responses.add(responses.POST, URL, body=requests.Timeout("timed out"))
    schedule()
    assert run(path)["days"][0]["status"] == "unknown"
    assert len(responses.calls) == 3
    schedule()
    assert run(path)["days"][0]["status"] == "unknown"
    assert len(responses.calls) == 4
    schedule()
    responses.add(responses.POST, URL, json={"code": 200})
    schedule([raw_order(report=report())])
    assert run(path, retry_unknown=True)["days"][0]["status"] == "submitted"


@responses.activate
def test_timeout_with_matching_readback_is_success(tmp_path):
    path = archive(tmp_path)
    schedule()
    responses.add(responses.POST, URL, body=requests.Timeout("timed out"))
    schedule([raw_order(report=report())])
    assert run(path)["days"][0]["status"] == "submitted"


@responses.activate
def test_success_response_with_different_saved_content_is_conflict(tmp_path):
    path = archive(tmp_path)
    schedule()
    responses.add(responses.POST, URL, json={"code": 200})
    schedule([raw_order(report=report(rate=40))])
    assert run(path)["days"][0]["status"] == "conflict"


@responses.activate
def test_business_failure_is_not_retried(tmp_path):
    path = archive(tmp_path)
    schedule()
    responses.add(responses.POST, URL, json={"code": 500, "msg": "不可提交"})
    schedule()
    assert run(path)["days"][0]["status"] == "failed"
    assert len(responses.calls) == 3


def test_report_lock_rejects_another_writer_and_releases(tmp_path):
    from git_daily_report.locking import report_lock
    path = tmp_path / "report.json"
    with report_lock(path):
        with pytest.raises(ValueError):
            with report_lock(path):
                pass
    with report_lock(path):
        pass


@responses.activate
def test_removed_local_order_blocks_day_instead_of_dropping_it(tmp_path):
    path = archive(tmp_path, [{"day": "2026-10-19", "orders": [local_order("1", 3), local_order("2", 4.5)]}])
    schedule([raw_order("2")])
    assert run(path)["days"][0]["status"] == "blocked"
    assert len(responses.calls) == 1


@responses.activate
def test_new_day_in_live_schedule_is_reported_as_blocked(tmp_path):
    path = archive(tmp_path)
    schedule([raw_order(report=report())], more=[{"time": "2026-10-20", "orders": [raw_order("2")]}])
    results = run(path)["days"]
    assert [(r["day"], r["status"]) for r in results] == [("2026-10-19", "skipped"), ("2026-10-20", "blocked")]


@responses.activate
def test_unknown_pauses_later_dates(tmp_path):
    path = archive(tmp_path, [
        {"day": "2026-10-19", "orders": [local_order(rate=65)]},
        {"day": "2026-10-20", "orders": [local_order(rate=70)]},
    ])
    schedule(more=[{"time": "2026-10-20", "orders": [raw_order()]}])
    responses.add(responses.POST, URL, body=requests.Timeout("timed out"))
    schedule()
    assert [r["status"] for r in run(path)["days"]] == ["unknown", "blocked"]
    assert len(responses.calls) == 3


@responses.activate
def test_malformed_success_response_is_unknown_and_never_retried(tmp_path):
    path = archive(tmp_path)
    schedule()
    responses.add(responses.POST, URL, json={})
    schedule()
    assert run(path)["days"][0]["status"] == "unknown"
    assert len(responses.calls) == 3


@responses.activate
def test_corrupt_pending_journal_cannot_enable_a_new_submission(tmp_path):
    path = archive(tmp_path)
    (path.parent / ".eom-submit.json").write_text('{"2026-10-19":{"status":"pending","rows":[{}]}}')
    schedule()
    with pytest.raises(ValueError, match="提交记录"):
        run(path)
    assert len(responses.calls) == 1


@responses.activate
def test_pending_journal_from_crashed_process_is_read_back_without_resending(tmp_path):
    path = archive(tmp_path)
    schedule()
    responses.add(responses.POST, URL, body=requests.Timeout("timed out"))
    schedule()
    assert run(path)["days"][0]["status"] == "unknown"
    journal_path = path.parent / ".eom-submit.json"
    journal = json.loads(journal_path.read_text())
    journal["2026-10-19"]["status"] = "pending"
    journal_path.write_text(json.dumps(journal))
    schedule([raw_order(report=report())])
    assert run(path)["days"][0]["status"] == "submitted"
    assert len(responses.calls) == 4
    assert "SECRET" not in journal_path.read_text()


@pytest.mark.parametrize("hours,rate", [(True, 50), (float("nan"), 50), (float("inf"), 50),
                                      (7.5, True), (7.5, 101), ("7.5", 50), (None, None)])
@responses.activate
def test_invalid_local_numbers_never_reach_commit(tmp_path, hours, rate):
    path = archive(tmp_path, [{"day": "2026-10-19", "orders": [local_order(hours=hours, rate=rate)]}])
    schedule()
    assert run(path)["days"][0]["status"] == "blocked"
    assert len(responses.calls) == 1


@responses.activate
def test_redirect_does_not_forward_credentials_or_repeat_the_post(tmp_path):
    path = archive(tmp_path)
    schedule()
    responses.add(responses.POST, URL, status=307, headers={"Location": "https://another.test/receive"})
    schedule()
    assert run(path)["days"][0]["status"] == "unknown"
    assert all(call.request.url == URL for call in responses.calls)


@pytest.mark.parametrize("identity", [{"ewdDate": "2026-10-20"}, {"ewlOrderId": "2"}, {"ewlOrderId": None}])
@responses.activate
def test_readback_conflicting_report_identity_is_not_success(tmp_path, identity):
    path = archive(tmp_path)
    schedule()
    responses.add(responses.POST, URL, json={"code": 200})
    schedule([raw_order(report={**report(), **identity})])
    assert run(path)["days"][0]["status"] == "conflict"


@responses.activate
def test_unknown_in_overlapping_archive_blocks_the_same_day(tmp_path):
    path = archive(tmp_path)
    schedule()
    responses.add(responses.POST, URL, body=requests.Timeout("timed out"))
    schedule()
    assert run(path)["days"][0]["status"] == "unknown"
    another_path = path.with_name("another-range.json")
    another_path.write_bytes(path.read_bytes())
    schedule()
    assert run(another_path)["days"][0]["status"] == "unknown"
    assert len(responses.calls) == 4


@responses.activate
def test_submissions_to_different_archives_share_a_directory_lock(tmp_path):
    from git_daily_report.locking import report_lock
    path = archive(tmp_path)
    with report_lock(path.parent / ".eom-submit"):
        with pytest.raises(ValueError, match="正被"):
            run(path)


@responses.activate
def test_business_rejection_with_matching_readback_is_saved_as_success(tmp_path):
    path = archive(tmp_path)
    schedule()
    responses.add(responses.POST, URL, json={"code": 500})
    schedule([raw_order(report=report())])
    assert run(path)["days"][0]["status"] == "submitted"


@responses.activate
def test_single_day_rate_cannot_fall_below_previous_filled_day(tmp_path):
    path = archive(tmp_path)
    schedule(more=[{"time": "2026-10-18", "orders": [raw_order(report=report(rate=90))]}])
    assert run(path, dry_run=True)["days"][0]["status"] == "blocked"
    assert len(responses.calls) == 1


@responses.activate
def test_single_day_rate_cannot_exceed_later_filled_day(tmp_path):
    path = archive(tmp_path, [{"day": "2026-10-19", "orders": [local_order(rate=80)]}])
    schedule(more=[{"time": "2026-10-21", "orders": [raw_order(report=report(rate=60))]}])
    assert run(path, dry_run=True)["days"][0]["status"] == "blocked"
    assert len(responses.calls) == 1


@responses.activate
def test_rate_anchor_from_previous_month_is_loaded_once_and_not_submitted(tmp_path):
    path = archive(tmp_path)
    current = {**raw_order(), "ewlStartTime": "2026-09-01 09:00:00"}
    schedule([current])
    schedule([raw_order(report=report(rate=90))], day="2026-09-30")
    results = run(path, dry_run=True)["days"]
    assert [(r["day"], r["status"]) for r in results] == [("2026-10-19", "blocked")]
    months = [json.loads(call.request.body)["condition"]["month"] for call in responses.calls]
    assert months == [10, 9]


def amend_order(rate=40, original=65):
    order = local_order(rate=rate)
    order["progress"] = {**order["progress"], "source": "amend", "reportId": "900", "originalRate": original}
    return order


@responses.activate
def test_amend_overwrites_only_the_rate_of_the_filled_report(tmp_path):
    path = archive(tmp_path, [{"day": "2026-10-19", "orders": [amend_order()]}])
    schedule([raw_order(report=report(hours=3, memo="具体任务：EOM 原文\n关键数据：无\n问题解决过程：无"))])
    responses.add(responses.POST, URL, json={"code": 200})
    schedule([raw_order(report=report(hours=3, rate=40, memo="具体任务：EOM 原文\n关键数据：无\n问题解决过程：无"))])
    assert run(path)["days"][0]["status"] == "submitted"
    row = json.loads(responses.calls[1].request.body)["condition"]["rows"][0]
    assert row["op"] == 8 and row["id"] == "900"
    assert row["ewdRate"] == 40 and row["ewdWorkRealTimes"] == 3
    assert row["memo"].startswith("具体任务：EOM 原文")
    from git_daily_report.store import load_days
    assert load_days(path)[0].orders[0].progress.source == "eom"


@responses.activate
def test_amend_is_blocked_when_eom_rate_changed_after_proposal(tmp_path):
    path = archive(tmp_path, [{"day": "2026-10-19", "orders": [amend_order()]}])
    schedule([raw_order(report=report(rate=50))])
    assert run(path)["days"][0]["status"] == "blocked"
    assert len(responses.calls) == 1


@responses.activate
def test_amend_timeout_with_old_rate_is_unknown_and_can_be_retried(tmp_path):
    path = archive(tmp_path, [{"day": "2026-10-19", "orders": [amend_order()]}])
    schedule([raw_order(report=report())])
    responses.add(responses.POST, URL, body=requests.Timeout("timed out"))
    schedule([raw_order(report=report())])
    assert run(path)["days"][0]["status"] == "unknown"
    schedule([raw_order(report=report())])
    responses.add(responses.POST, URL, json={"code": 200})
    schedule([raw_order(report=report(rate=40))])
    assert run(path, retry_unknown=True)["days"][0]["status"] == "submitted"
