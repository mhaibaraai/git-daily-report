"""预检并逐日提交 EOM；写前留存意图，写后回读，未知结果不自动重发。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

from .config import EomConfig
from .locking import report_lock
from .models import ScheduleDay
from .sources._http import SourceError
from .sources.eom import EomAuthError, EomClient, EomError, _order
from .store import load_days, save_days
from .validate import check_numbers, number, validate_days


_METADATA = {
    "ewdTitle": "ewlTitle", "ewdWorkPlanTimes": "ewlWorkPlanTimes",
    "ewdWorker": "ewlWorker", "ewdWorkerName": "ewlWorkerName", "ewdDept": "ewlDept",
    "ewdNum": "ewlNum", "ewdStartTime": "ewlStartTime", "ewdEndTime": "ewlEndTime",
    "ewlOrderId": "id", "eProjectId": "eProjectId", "eTaskId": "eTaskId",
    "eProjectName": "eProjectName", "eTaskTitle": "eTaskTitle", "type": "type",
}
_IDS = {"ewdWorker", "ewdDept", "ewlOrderId", "eProjectId", "eTaskId"}
_TEXT = ("memo", "ewdProblem", "tomorrowPlan")


def submit_report(
    cfg: EomConfig, path: Path, since: date, until: date, *,
    dry_run: bool = False, retry_unknown: bool = False,
) -> dict:
    """提交当前报告。只需要 EOM 配置；dry_run 只读，不更新报告或 journal。"""
    if not cfg.base_url or not cfg.token:
        raise ValueError("请配置 EOM_BASE_URL 与 EOM_TOKEN")
    if since > until:
        raise ValueError("开始日期不能晚于结束日期")
    with report_lock(path), report_lock(path.parent / ".eom-submit"):
        local = [day for day in load_days(path) if since.isoformat() <= day.day <= until.isoformat()]
        if not local:
            raise ValueError("没有可提交的本地报告，请先生成")
        _check_keys(local)
        client = EomClient(cfg)
        fresh = client.raw_schedule(since, until)
        merged = validate_days(_merge(local, fresh))
        journal_path = path.parent / ".eom-submit.json"
        journal = _read_journal(journal_path)
        results = []
        stopped = False
        for day in sorted(merged, key=lambda item: item.day):
            if stopped:
                results.append(_result(day.day, "blocked", "前一日提交结果待核实，暂停后续提交"))
                continue
            entry = journal.get(day.day)
            if entry and entry.get("status") in {"pending", "unknown", "conflict"}:
                state = _verify(entry["rows"], fresh.get(day.day, []))
                if state == "submitted":
                    journal.pop(day.day)
                    results.append(_result(day.day, state, "已回读确认此前提交"))
                    continue
                if state == "conflict" or not retry_unknown or _has_any_report(entry["rows"], fresh.get(day.day, [])):
                    results.append(_result(day.day, state, "此前提交结果待核实；请核对 EOM 后主动重试"))
                    stopped = True
                    continue
            rows, error = _prepare(day, fresh.get(day.day, []))
            if error:
                results.append(_result(day.day, "blocked", error))
                continue
            if not rows:
                results.append(_result(day.day, "skipped", "EOM 已填，无需重复提交"))
                continue
            if entry and entry.get("status") in {"pending", "unknown", "conflict"} and rows != entry["rows"]:
                results.append(_result(day.day, "conflict", "本地内容与待核实批次不同，请先核对此前提交"))
                stopped = True
                continue
            if dry_run:
                results.append({**_result(day.day, "ready", "预检通过，尚未提交"), "orders": len(rows)})
                continue
            journal[day.day] = {"status": "pending", "rows": rows}
            _write_journal(journal_path, journal)
            rejection = None
            try:
                client.commit_report(rows)
            except EomAuthError as exc:
                journal.pop(day.day)
                _write_journal(journal_path, journal)
                results.append(_result(day.day, "failed", str(exc)))
                continue
            except EomError as exc:
                rejection = str(exc)
            except SourceError:
                pass
            try:
                readback = client.raw_schedule(date.fromisoformat(day.day), date.fromisoformat(day.day))
                fresh[day.day] = readback.get(day.day, [])
                state = _verify(rows, fresh[day.day])
                if state == "unknown" and rejection and not _has_any_report(rows, fresh[day.day]):
                    state = "failed"
            except SourceError:
                state = "unknown"
            if state in {"submitted", "failed"}:
                journal.pop(day.day)
            else:
                journal[day.day]["status"] = state
                stopped = True
            _write_journal(journal_path, journal)
            results.append(_result(day.day, state, {
                "submitted": "已提交并回读确认", "unknown": "提交结果未知，已暂停；请先核对 EOM",
                "conflict": "EOM 已填内容与本次提交不同，请核对",
                "failed": rejection,
            }[state]))
        if not dry_run:
            _write_journal(journal_path, journal)
            refreshed = validate_days(_merge(local, fresh))
            by_day = {day.day: day for day in refreshed}
            saved = [by_day.get(day.day, day) for day in load_days(path)]
            save_days(path, since, until, saved)
        return {"days": results}


def _check_keys(days: list[ScheduleDay]) -> None:
    seen = set()
    for day in days:
        try:
            date.fromisoformat(day.day)
        except ValueError as exc:
            raise ValueError("本地报告包含非法日期") from exc
        if day.day in seen:
            raise ValueError("本地报告包含重复日期")
        seen.add(day.day)
        ids = [_identifier(order.id) for order in day.orders]
        if len(ids) != len(set(ids)):
            raise ValueError("本地报告包含重复工单")


def _merge(local: list[ScheduleDay], fresh: dict[str, list[dict]]) -> list[ScheduleDay]:
    merged = []
    local_by_day = {day.day: day for day in local}
    for day_key in sorted(set(local_by_day) | set(fresh)):
        day = local_by_day.get(day_key, ScheduleDay(day_key))
        saved = {order.id: order for order in day.orders}
        orders = []
        for raw in fresh.get(day.day, []):
            order = _order(raw)
            previous = saved.get(order.id)
            if order.progress is None and previous and previous.progress and previous.progress.source == "ai":
                order = replace(order, progress=previous.progress)
            elif _pending_amend(order, previous):
                order = replace(order, progress=replace(
                    order.progress, source="amend", rate=previous.progress.rate,
                    original_rate=previous.progress.original_rate,
                ))
            orders.append(order)
        live_ids = {order.id for order in orders}
        orders.extend(order for order in day.orders if order.id not in live_ids)
        merged.append(ScheduleDay(day.day, tuple(orders)))
    return merged


def _pending_amend(live, saved) -> bool:
    """本地修正尚未写入：EOM 仍是同一份日报且百分比与修正值不同。"""
    return (saved is not None and saved.progress is not None and saved.progress.source == "amend"
            and live.progress is not None and live.progress.report_id == saved.progress.report_id
            and number(live.progress.rate) != number(saved.progress.rate))


def _prepare(day: ScheduleDay, raw_orders: list[dict]) -> tuple[list[dict], str | None]:
    if date.fromisoformat(day.day) > date.today():
        return [], "不能提交未来日期"
    if not raw_orders:
        return [], "实时排期中没有该日期的工单，请刷新报告"
    try:
        raw_by_id = {_identifier(raw.get("id")): raw for raw in raw_orders}
        if len(raw_by_id) != len(raw_orders):
            return [], "实时排期包含重复工单"
        rows = []
        for order in day.orders:
            p = order.progress
            if p is None:
                return [], "存在未生成的工单进展，请先补齐"
            if order.id not in raw_by_id:
                return [], "排期已变化，本地存在已移除工单，请刷新报告"
            if p.source == "eom":
                continue
            if p.source not in {"ai", "amend"} or p.problems:
                return [], "存在未通过校验的工单进展，请先修正"
            if p.source == "amend":
                row, error = _amend_row(day.day, p, raw_by_id[order.id])
                if error:
                    return [], error
                rows.append(row)
                continue
            row = _metadata(raw_by_id[order.id])
            row.update({"memo": p.memo, "ewdProblem": p.issue, "tomorrowPlan": p.plan,
                        "ewdWorkRealTimes": float(number(p.hours)), "ewdRate": float(number(p.rate)),
                        "ewdDate": day.day, "status": 1, "op": 4})
            rows.append(row)
        return rows, None
    except (ValueError, TypeError, KeyError):
        return [], "EOM 排期缺少合法的提交元数据，请刷新后重试"


def _amend_row(day: str, p, raw: dict) -> tuple[dict, str | None]:
    """按日报 ID 覆盖：文字与工时取 EOM 当前值，只替换百分比。"""
    report = raw.get("report")
    if not isinstance(report, dict) or str(report.get("id") or "") != p.report_id:
        return {}, "EOM 中待修正的日报已不存在，请重新生成修正"
    if number(report.get("ewdRate")) != number(p.original_rate):
        return {}, "EOM 已填百分比在修正后发生变化，请重新生成修正"
    hours = number(report.get("ewdWorkRealTimes"))
    if hours is None or not all(isinstance(report.get(key), str) for key in _TEXT):
        return {}, "EOM 已填日报缺少工时或文字，无法修正"
    row = _metadata(raw)
    row.update({key: report[key] for key in _TEXT})
    row.update({"ewdWorkRealTimes": float(hours), "ewdRate": float(number(p.rate)),
                "ewdDate": day, "status": 1, "op": 8, "id": p.report_id})
    return row, None


def _identifier(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ValueError("工单标识非法")
    return str(value)


def _metadata(raw: dict) -> dict:
    row = {}
    for target, source in _METADATA.items():
        value = raw.get(source)
        if target in _IDS:
            value = _identifier(value)
        elif target == "ewdWorkPlanTimes":
            parsed = number(value)
            if parsed is None or parsed < 0:
                raise ValueError("计划工时非法")
        elif target == "type":
            if type(value) is not int or value not in {1, 2}:
                raise ValueError("工单类型非法")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError("提交元数据缺失")
        row[target] = value
    return row


def _has_any_report(rows: list[dict], orders: list[dict]) -> bool:
    """是否已有部分写入；修正行以百分比已变为修正值为准。"""
    reports = {str(raw.get("id")): raw.get("report") for raw in orders}
    for row in rows:
        report = reports.get(row["ewlOrderId"])
        if not isinstance(report, dict) or not report.get("id"):
            continue
        if row.get("op") != 8 or number(report.get("ewdRate")) == number(row["ewdRate"]):
            return True
    return False


def _verify(rows: list[dict], raw_orders: list[dict]) -> str:
    reports = {str(raw.get("id")): raw.get("report") for raw in raw_orders}
    complete = True
    for row in rows:
        report = reports.get(row["ewlOrderId"])
        if not isinstance(report, dict) or not report.get("id"):
            complete = False
            continue
        if report.get("ewdDate") != row["ewdDate"]:
            return "conflict"
        try:
            if _identifier(report.get("ewlOrderId")) != row["ewlOrderId"]:
                return "conflict"
        except ValueError:
            return "conflict"
        if any(report.get(key) != row[key] for key in _TEXT):
            return "conflict"
        if number(report.get("ewdWorkRealTimes")) != number(row["ewdWorkRealTimes"]):
            return "conflict"
        if number(report.get("ewdRate")) != number(row["ewdRate"]):
            if row.get("op") == 8:
                # 修正前的日报仍在，无法区分未写入与被他人改动，交由提交前的原值校验
                complete = False
                continue
            return "conflict"
    return "submitted" if complete else "unknown"


def _result(day: str, status: str, message: str) -> dict:
    return {"day": day, "status": status, "message": message}


def _read_journal(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(journal, dict) or any(
            not isinstance(entry, dict) or not isinstance(entry.get("rows"), list)
            or not entry["rows"] for entry in journal.values()
        ):
            raise ValueError("invalid journal")
        for day, entry in journal.items():
            date.fromisoformat(day)
            if entry.get("status") not in {"pending", "unknown", "conflict"}:
                raise ValueError("invalid journal status")
            for row in entry["rows"]:
                if not isinstance(row, dict) or row.get("ewdDate") != day:
                    raise ValueError("invalid journal row")
                if not set(_METADATA).issubset(row) or not all(isinstance(row.get(k), str) for k in _TEXT):
                    raise ValueError("incomplete journal row")
                _identifier(row["ewlOrderId"])
                if check_numbers(row.get("ewdRate"), row.get("ewdWorkRealTimes")):
                    raise ValueError("invalid journal numbers")
        return journal
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("提交记录无法读取；请先核对 EOM，避免重复提交") from exc


def _write_journal(path: Path, journal: dict) -> None:
    handle = tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8")
    try:
        with handle:
            json.dump(journal, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    finally:
        Path(handle.name).unlink(missing_ok=True)
