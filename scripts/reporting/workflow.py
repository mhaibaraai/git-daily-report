"""Material collection, non-destructive draft updates and report rendering."""

import copy
from datetime import datetime, timedelta

from collect_git_activity import ReportPeriod
from .common import ReportError, keys, number, text
from .archive import archive_report
from .eom import EomClient, normalize
from .gitlab import GitlabClient
from .store import SCHEMA_VERSION, load_report, lock, report_path, write_state
from .validation import generated_progress, input_items, summary, validate_days


def merge_days(saved, fresh):
    previous = {day["day"]: {order["id"]: order for order in day["orders"]} for day in saved}
    result = []
    for day in sorted(set(previous) | set(fresh)):
        old = previous.get(day, {})
        orders = []
        for raw in fresh.get(day, []):
            order = normalize(raw)
            local = old.get(order["id"], {}).get("progress")
            live = order["progress"]
            if local and local["source"] == "revise":
                from .revisions import matches, snapshot
                if not live or not matches(snapshot(live), snapshot(local)):
                    order["progress"] = copy.deepcopy(local)
            elif local and local["source"] == "agent" and not live:
                order["progress"] = copy.deepcopy(local)
            elif (local and local["source"] == "amend" and live
                  and live["reportId"] == local.get("reportId")
                  and number(live["rate"]) != number(local["rate"])):
                order["progress"] = {**live, "source": "amend", "rate": local["rate"],
                                     "originalRate": local["originalRate"], "estimatedFields": local.get("estimatedFields", ["rate"])}
            orders.append(order)
        present = {order["id"] for order in orders}
        orders.extend({**copy.deepcopy(order), "stale": True} for key, order in old.items() if key not in present)
        result.append({"day": day, "orders": orders})
    return result


def _matched_mappings(profile, days):
    result, found = [], {}
    selectors = (("order_id", "id"), ("order_title", "title"), ("project_name", "projectName"))
    for day in days:
        for order in day["orders"]:
            if order.get("stale"):
                continue
            for config_key, order_key in selectors:
                matches = [mapping for mapping in profile.mappings
                           if mapping.get(config_key) == order.get(order_key)]
                if not matches:
                    continue
                for mapping in matches:
                    target = {"id": mapping["gitlab_project"]}
                    if mapping.get("ref"):
                        target["ref"] = mapping["ref"]
                    result.append(target)
                    found.setdefault(order["id"], []).append(target)
                break
    unique = {(target["id"], target.get("ref")): target for target in result}
    return list(unique.values()), found


def _new_report(profile, span, mode):
    return {"schemaVersion": SCHEMA_VERSION, "mode": mode, "scope": profile.scope(mode),
            "range": {"since": str(span.since), "until": str(span.until), "timezone": profile.timezone},
            "days": [], "commits": [], "warnings": [], "markdown": "", "dailyHours": profile.daily_hours}


def _refresh(report, profile, span, client, discard_stale):
    validate_days([], profile.daily_hours)
    fresh = client.schedule(span.since, span.until, today=datetime.now(span.timezone).date())
    days = merge_days(report["days"], fresh)
    if discard_stale:
        archived = report.setdefault("archivedOrders", [])
        for day in days:
            archived.extend({"day": day["day"], "order": order} for order in day["orders"] if order.get("stale"))
            day["orders"] = [order for order in day["orders"] if not order.get("stale")]
        days = [day for day in days if day["orders"]]
    report["days"] = validate_days(days, profile.daily_hours)


def material(profile, span, *, mode="work-orders", gitlab=None, eom=None, discard_stale=False):
    path = report_path(profile, span, mode)
    with lock(path):
        report = load_report(profile, span, mode, optional=True) or _new_report(profile, span, mode)
        today = datetime.now(span.timezone).date()
        targets, mappings = [], {}
        if mode == "work-orders":
            validate_days([], profile.daily_hours)
            client = eom or EomClient(profile.eom)
            _refresh(report, profile, span, client, discard_stale)
            targets, mappings = _matched_mappings(profile, report["days"])
        pending = [{"day": day["day"], "orderId": order["id"]} for day in report["days"]
                   for order in day["orders"] if not order.get("stale") and (
                       not order.get("progress") or (order["progress"]["source"] == "agent" and order["progress"].get("problems")))]
        if mode == "gitlab" or any(day["orders"] for day in report["days"]):
            client = gitlab or GitlabClient(profile.gitlab)
            slack = profile.commit_slack_days if mode == "work-orders" else 0
            window = ReportPeriod(span.since - timedelta(days=slack),
                                  min(span.until + timedelta(days=slack), today), span.timezone)
            collected = client.collect(window, projects=targets) if window.since <= window.until else {"commits": [], "warnings": []}
            report["commits"] = collected["commits"]
            report["warnings"] = collected.get("warnings", [])
            report["authorFiltered"] = collected.get("authorFiltered", True)
        else:
            report["commits"], report["warnings"] = [], []
        report["dailyHours"] = profile.daily_hours
        report["mappings"] = mappings
        write_state(path, report)
    return {"path": str(path), "mode": mode, "dailyHours": profile.daily_hours, "pending": pending,
            "validation": summary(report["days"]),
            "material": {"schedule": report["days"], "commits": report["commits"], "mappings": mappings},
            "authorFiltered": report.get("authorFiltered", True), "warnings": report["warnings"]}


def apply_items(profile, span, payload, *, mode="work-orders"):
    path = report_path(profile, span, mode)
    with lock(path):
        report = load_report(profile, span, mode)
        if mode == "gitlab":
            keys(payload, {"markdown"}, {"markdown"})
            report["markdown"] = text(payload["markdown"], "markdown")
            write_state(path, report)
            return {"path": str(path), "reviewPath": archive_report(profile, report),
                    "passed": 1, "failures": [], "missing": []}
        index = {(day["day"], order["id"]): order for day in report["days"] for order in day["orders"]}
        rejected = []
        for key, item in input_items(payload):
            order = index.get(key)
            try:
                if order is None or order.get("stale"):
                    raise ReportError("Input contains a work order outside the current schedule.")
                if order.get("progress") and order["progress"]["source"] != "agent":
                    raise ReportError("apply cannot replace an existing EOM report or correction.")
                order["progress"] = generated_progress(item)
            except ReportError as exc:
                rejected.append({"day": key[0], "orderId": key[1], "problems": [str(exc)]})
        report["days"] = validate_days(report["days"], profile.daily_hours)
        result = summary(report["days"])
        result["failures"].extend(rejected)
        write_state(path, report)
        return {"path": str(path), "reviewPath": archive_report(profile, report), **result}


def amend(profile, span, payload, *, client=None):
    path = report_path(profile, span)
    with lock(path):
        report = load_report(profile, span)
        client = client or EomClient(profile.eom)
        fresh = client.schedule(span.since, span.until, today=datetime.now(span.timezone).date())
        report["days"] = merge_days(report["days"], fresh)
        index = {(day["day"], order["id"]): order for day in report["days"] for order in day["orders"]}
        live = {(day, str(raw["id"])): raw for day, orders in fresh.items() for raw in orders}
        changes = []
        for key, entry in input_items(payload, correction=True):
            order = index.get(key)
            progress = normalize(live[key])["progress"] if key in live else None
            rate = number(entry["rate"])
            if not progress or progress["source"] not in {"eom", "amend"} or order.get("stale"):
                raise ReportError("Corrections require an existing EOM report in the current schedule.")
            if rate is None or not 0 <= rate <= 100:
                raise ReportError("Corrected progress must be a finite number from 0 to 100.")
            original = progress.get("originalRate", progress["rate"])
            if number(original) is None:
                raise ReportError("Original EOM progress is missing or invalid.")
            order["progress"] = progress
            if rate == number(original):
                progress.update(source="eom", rate=original, estimatedFields=[])
                progress.pop("originalRate", None)
            else:
                progress.update(source="amend", originalRate=original, rate=float(rate), estimatedFields=["rate"])
                changes.append({"day": key[0], "orderId": key[1], "from": original, "to": float(rate)})
        report["days"] = validate_days(report["days"], profile.daily_hours)
        result = summary(report["days"])
        write_state(path, report)
        return {"path": str(path), "reviewPath": archive_report(profile, report), "changes": changes, **result}


def export_markdown(report):
    if report["mode"] == "gitlab":
        return report.get("markdown", "")
    bounds = report["range"]
    lines = ["# 工作进展", "", f"时间范围：{bounds['since']} 至 {bounds['until']}（{bounds['timezone']}）", ""]
    for day in report["days"]:
        lines.extend(["## " + day["day"], ""])
        for order in day["orders"]:
            lines.extend(["### " + order["title"], ""])
            progress = order.get("progress")
            if not progress:
                lines.extend(["待补充事实素材。", ""])
                continue
            lines.extend([progress.get("memo", ""), "", "存在问题：" + progress.get("issue", ""),
                          "明日计划：" + progress.get("plan", ""), ""])
            qualifier = "（估算）" if progress.get("estimatedFields") else ""
            lines.extend([f"进度：{progress.get('rate')}%；工时：{progress.get('hours')}h{qualifier}", ""])
            if progress.get("problems"):
                lines.extend(["待复核：" + "；".join(progress["problems"]), ""])
    if report.get("warnings"):
        lines.extend(["取材限制：" + "；".join(report["warnings"]), ""])
    return "\n".join(lines)
