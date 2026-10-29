from dataclasses import replace

import pytest

from git_daily_report.models import Order, Progress, ScheduleDay
from git_daily_report import validate


def progress(rate=40, hours=3, source="ai"):
    return Progress("具体任务：完成核对\n关键数据：无\n问题解决过程：无", "无", "继续核对", source,
                    rate=rate, hours=hours)


def day(value="2026-10-26", *values):
    return ScheduleDay(value, tuple(Order(str(i), "核对", "任务", "项目", p)
                                   for i, p in enumerate(values)))


def test_progress_numbers_round_trip_and_old_archives_remain_readable():
    p = progress(65, 4.5)
    assert Progress.from_dict(p.to_dict()) == p
    old = Progress.from_dict({"memo": "原有内容", "source": "ai"})
    assert old.rate is None and old.hours is None


@pytest.mark.parametrize("rate,hours", [(0, .1), (100, 7.5), (65.5, 3)])
def test_valid_numbers(rate, hours):
    assert validate.check_numbers(rate, hours) == []


@pytest.mark.parametrize("value", [None, True, "3", "bad", float("nan"), float("inf"), -1])
def test_invalid_hours(value):
    assert any("工时" in p for p in validate.check_numbers(50, value))


@pytest.mark.parametrize("value", [None, False, "50", float("nan"), -1, 101])
def test_invalid_rates(value):
    assert any("进展" in p for p in validate.check_numbers(value, 3))


def test_daily_limit_counts_eom_hours_and_revalidates_saved_blocks():
    original = day("2026-10-26", progress(40, 4.5, "eom"), progress(30, 3))
    assert not validate.validate_days([original])[0].orders[1].progress.problems
    changed = replace(original, orders=(original.orders[0], replace(original.orders[1], progress=progress(30, 3.1))))
    flagged = validate.validate_days([changed])[0].orders[1].progress
    assert any("7.5" in p for p in flagged.problems)
    restored = replace(changed, orders=(changed.orders[0], replace(changed.orders[1], progress=replace(flagged, hours=3))))
    assert validate.validate_days([restored])[0].orders[1].progress.problems == ()


def test_unknown_eom_hours_prevent_remaining_blocks_being_submitted():
    result = validate.validate_days([day("2026-10-26", progress(40, None, "eom"), progress(20, 3))])
    assert any("已填工时" in p for p in result[0].orders[1].progress.problems)


def test_decimal_sums_do_not_exceed_limit_due_to_float_noise():
    result = validate.validate_days([day("2026-10-26", progress(10, .1), progress(10, .2), progress(10, 7.2))])
    assert all(not o.progress.problems for o in result[0].orders)


def test_rate_regression_checks_existing_and_generated_days():
    previous = day("2026-10-25", progress(60, 7.5, "eom"))
    generated = day("2026-10-26", progress(50, 7.5))
    later = day("2026-10-27", progress(55, 7.5, "eom"))
    result = validate.validate_days([later, generated, previous])
    assert any("倒退" in p for p in result[1].orders[0].progress.problems)


def test_generated_rate_cannot_exceed_a_later_eom_anchor():
    generated = day("2026-10-26", progress(80, 7.5))
    later = day("2026-10-27", progress(60, 7.5, "eom"))
    result = validate.validate_days([generated, later])
    assert any("倒退" in p for p in result[0].orders[0].progress.problems)


def test_malformed_task_is_reported_without_crashing():
    assert validate.check_block({"task": 12, "plan": "核对", "rate": 50, "hours": 3})


def test_invalid_numeric_output_remains_valid_json_for_browser():
    import json
    p = progress(float("nan"), float("inf"))
    encoded = json.dumps(p.to_dict(), allow_nan=False)
    assert json.loads(encoded)["hours"] is None


def test_saved_progress_requires_all_three_memo_sections():
    malformed = replace(progress(), memo="具体任务：完成核对")
    result = validate.validate_days([day("2026-10-26", malformed)])
    assert any("三段" in p for p in result[0].orders[0].progress.problems)


@pytest.mark.parametrize("rate,minimum,maximum", [(65, 90, None), (80, None, 60)])
def test_rate_bounds_from_reports_outside_selected_range(rate, minimum, maximum):
    order = Order("1", "核对", "任务", "项目", progress(rate, 7.5),
                  rate_min=minimum, rate_max=maximum)
    result = validate.validate_days([ScheduleDay("2026-10-26", (order,))])
    assert any("倒退" in p for p in result[0].orders[0].progress.problems)
    assert Order.from_dict(order.to_dict()) == order


def test_distinct_order_ids_are_independent_even_with_a_legacy_shared_continuity_id():
    old = Order("old", "核对", "任务", "项目", progress(80, 7.5), continuity_id="old")
    new = Order("new", "核对", "任务", "项目", progress(65, 7.5), continuity_id="old")
    days = [ScheduleDay("2026-10-20", (old,)), ScheduleDay("2026-10-21", (new,))]
    result = validate.validate_days(days)
    assert not result[1].orders[0].progress.problems
    assert result[1].orders[0].id == "new"
    restored = Order.from_dict(new.to_dict())
    assert restored.id == "new" and restored.progress.rate == 65


def test_legacy_cross_id_rate_bounds_are_discarded_without_changing_saved_rate():
    saved = Order("new", "核对", "任务", "项目", progress(20, 7.5), rate_min=80,
                  previous_day="2026-10-20", previous_memo="其他 ID 的工作", continuity_id="old")
    restored = Order.from_dict(saved.to_dict())
    assert restored.rate_min is None and restored.rate_max is None
    assert restored.previous_day == "" and restored.previous_memo == ""
    assert restored.progress.rate == 20
    assert not validate.validate_days([ScheduleDay("2026-10-21", (restored,))])[0].orders[0].progress.problems
