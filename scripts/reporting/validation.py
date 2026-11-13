"""Validate generated inputs and recompute whole-day and same-order constraints."""

import copy

from .common import ReportError, day_string, identifier, keys, number, text


SECTIONS = {"task": "具体任务", "data": "关键数据", "problem": "问题解决过程"}
TEXT_KEYS = ("memo", "issue", "plan")
GENERATION_NOTES = (
    "按用户要求依据本周素材", "按用户要求进行主题分配", "非实际提交日期",
    "非实际提交日期或实测工时", "不代表当日提交", "实际工作日期、投入与完成进度待核实",
    "按本周集成素材归并", "本方案为本次整理形成", "尚无当日实施记录",
)


def business_text_errors(progress):
    return ["Generation notes are not allowed in EOM business field: " + key
            for key in TEXT_KEYS if isinstance(progress.get(key), str)
            and any(note in progress[key] for note in GENERATION_NOTES)]


def input_items(payload, *, correction=False):
    keys(payload, {"items"}, {"items"})
    if not isinstance(payload["items"], list):
        raise ReportError("items must be an array.")
    result, seen = [], set()
    allowed = {"day", "orderId", "rate"} if correction else {
        "day", "orderId", "task", "data", "problem", "issue", "plan", "rate", "hours",
        "estimatedFields", "planSuggested",
    }
    for item in payload["items"]:
        keys(item, allowed, {"day", "orderId", "rate"})
        key = (day_string(item["day"]), identifier(item["orderId"]))
        if item["day"] != key[0] or key in seen:
            raise ReportError("items contains duplicate or noncanonical day/order keys.")
        seen.add(key)
        result.append((key, item))
    return result


def generated_progress(item):
    values = {}
    for key in SECTIONS:
        rows = item.get(key, [])
        if not isinstance(rows, list) or any(not isinstance(row, str) or not row.strip() for row in rows):
            raise ReportError("Report sections must be arrays of nonempty text.")
        values[key] = [row.strip() for row in rows]
    if not values["task"] or values["task"] == ["无"]:
        raise ReportError("Concrete work must be provided; leave unsupported orders pending.")
    rate, hours = number(item.get("rate")), number(item.get("hours"))
    if rate is None or not 0 <= rate <= 100 or hours is None or not 0 < hours <= 24:
        raise ReportError("rate must be 0–100 and hours must be positive finite JSON numbers no greater than 24.")
    estimated = item.get("estimatedFields", ["rate", "hours"])
    if not isinstance(estimated, list) or any(not isinstance(key, str) or key not in {"rate", "hours"} for key in estimated):
        raise ReportError("estimatedFields may contain only rate and hours.")
    if type(item.get("planSuggested", False)) is not bool:
        raise ReportError("planSuggested must be a boolean.")
    issue = item.get("issue", "")
    if not isinstance(issue, str):
        raise ReportError("issue must be text.")
    result = {"source": "agent", "memo": "\n".join(
                label + "：" + "；".join(values[key] or ["无"]) for key, label in SECTIONS.items()),
            "issue": issue.strip() or "无", "plan": text(item.get("plan"), "plan"),
            "rate": float(rate), "hours": float(hours), "estimatedFields": list(dict.fromkeys(estimated)),
            "planSuggested": item.get("planSuggested", False)}
    errors = business_text_errors(result)
    if errors:
        raise ReportError("; ".join(errors))
    return result


def _progress_errors(order, progress):
    problems = business_text_errors(progress) if progress["source"] != "eom" else []
    if progress["source"] != "eom" and any(
        not isinstance(progress.get(key), str) or not progress[key].strip() for key in TEXT_KEYS
    ):
        problems.append("Required report text is missing.")
    rate, hours = number(progress.get("rate")), number(progress.get("hours"))
    if rate is None or not 0 <= rate <= 100:
        problems.append("Cumulative progress must be a finite number from 0 to 100.")
    if hours is None or hours <= 0:
        problems.append("Hours must be a positive finite number.")
    if progress.get("planSuggested"):
        problems.append("The proposed next step must be confirmed before submission.")
    if order.get("stale"):
        problems.append("This work order no longer exists in the current schedule.")
    if rate is not None and progress["source"] == "agent":
        low, high = number(order.get("rateMin")), number(order.get("rateMax"))
        if (low is not None and rate < low) or (high is not None and rate > high):
            problems.append("Progress conflicts with the same order's historical bounds.")
    return problems


def validate_days(days, daily_hours):
    result = copy.deepcopy(days)
    budget = number(daily_hours)
    if budget is None or not 0 < budget <= 24:
        raise ReportError("Configure daily_hours before generating or submitting work orders.")
    histories, current = {}, set()
    for day in result:
        for order in day["orders"]:
            current.add((day["day"], order["id"]))
            progress = order.get("progress")
            if progress:
                progress["problems"] = _progress_errors(order, progress)
                rate = number(progress.get("rate"))
                if rate is not None:
                    histories.setdefault(order["id"], {})[day["day"]] = rate
    for day in result:
        _daily_errors(day, budget)
        for order in day["orders"]:
            for anchor in order.get("rateAnchors", []):
                rate = number(anchor.get("rate"))
                if (anchor.get("day"), order["id"]) not in current and rate is not None:
                    histories.setdefault(order["id"], {})[anchor["day"]] = rate
    for day in result:
        for order in day["orders"]:
            progress = order.get("progress")
            if not progress:
                continue
            rate = number(progress.get("rate"))
            for other_day, other_rate in histories.get(order["id"], {}).items():
                if rate is not None and ((other_day < day["day"] and other_rate > rate)
                                         or (other_day > day["day"] and other_rate < rate)):
                    progress["problems"].append("The same order's cumulative progress cannot decrease across dates.")
                    break
    return result


def _daily_errors(day, budget):
    total, invalid = number(0), False
    for order in day["orders"]:
        progress = order.get("progress")
        if not progress:
            continue
        hours = number(progress.get("hours"))
        if hours is None or hours <= 0:
            invalid = True
        else:
            total += hours
    for order in day["orders"]:
        if order.get("progress"):
            if invalid:
                order["progress"]["problems"].append("Existing hours are missing or invalid; daily capacity cannot be verified.")
            if total > budget:
                order["progress"]["problems"].append("Total daily hours exceed the configured budget.")


def summary(days):
    failures, missing, passed = [], [], 0
    for day in days:
        for order in day["orders"]:
            key = {"day": day["day"], "orderId": order["id"]}
            progress = order.get("progress")
            if not progress:
                missing.append(key)
            elif progress.get("problems"):
                failures.append({**key, "problems": progress["problems"]})
            elif progress["source"] in {"agent", "amend", "revise"}:
                passed += 1
    return {"passed": passed, "failures": failures, "missing": missing}
