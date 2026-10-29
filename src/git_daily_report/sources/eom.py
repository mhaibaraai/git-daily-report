"""EOM 日程表读取。

接口取自 EOM 前端「日程表」页面：所有业务请求都走 /core/busi/exec，靠 condition.op
区分操作，op=schedule 按年月返回每天排了哪些工单以及已填的进展。服务端按 menuId
校验菜单权限，前端发请求前会把它换成当前页面的菜单 ID，日程表页面是 ScheduleP。节假日与调休
休息日在日程表上没有工单，这里不另做日历判断。

提交使用 commitReport，写请求只发一次，结果由上层回读核实。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterator

from ..config import EomConfig
from ..models import Order, Progress, ScheduleDay
from ..validate import number
from ._http import SourceError, request


class EomError(SourceError):
    """EOM 接口返回错误。"""


class EomAuthError(EomError):
    """token 失效，需要回企微里重新取一次。"""


_AUTH_HINT = "EOM token 已失效，请在企微内重新打开 EOM 取 token 并更新 .env 的 EOM_TOKEN"


class EomClient:
    def __init__(self, cfg: EomConfig) -> None:
        self._cfg = cfg

    def schedule(self, since: date, until: date, today: date | None = None) -> list[ScheduleDay]:
        """区间内排了工单的日子，今天之后的不算——EOM 也不让填未来。"""
        return [
            ScheduleDay(day, tuple(_order(o) for o in orders))
            for day, orders in self.raw_schedule(since, until, today).items()
        ]

    def raw_schedule(
        self, since: date, until: date, today: date | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """保留提交所需的排期字段；调用方仅白名单映射，不交给 AI。"""
        now = today or date.today()
        days, cache = self._load(since, until, now)
        return _with_rate_bounds(days, cache, now.isoformat())

    def continuity(
        self, since: date, until: date, today: date | None = None
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
        """带进度上下界的排期，以及每个工单 ID 自己的全部排期日（含未来）。"""
        now = today or date.today()
        days, cache = self._load(since, until, now)
        selected = {str(order.get("id")) for orders in days.values() for order in orders}
        while True:
            ends = [
                end for _, order in _calendar_orders(cache)
                if str(order.get("id")) in selected
                and (end := _day_of({"time": order.get("ewlEndTime")}))
            ]
            last = max(ends, default=until.isoformat())
            missing = [month for month in _months(since, date.fromisoformat(last)) if month not in cache]
            if not missing:
                break
            for year, month in missing:
                cache[(year, month)] = self._month(year, month)
        scheduled: dict[str, set[str]] = {}
        for day, order in _calendar_orders(cache):
            order_id = str(order.get("id"))
            if order_id in selected:
                scheduled.setdefault(order_id, set()).add(day)
        return (_with_rate_bounds(days, cache, now.isoformat()),
                {order_id: sorted(found) for order_id, found in scheduled.items()})

    def _load(
        self, since: date, until: date, now: date
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[tuple[int, int], list[dict[str, Any]]]]:
        high = min(until, now).isoformat()
        low = since.isoformat()
        days: dict[str, list[dict[str, Any]]] = {}
        cache: dict[tuple[int, int], list[dict[str, Any]]] = {}

        def month_rows(year: int, month: int) -> list[dict[str, Any]]:
            key = (year, month)
            if key not in cache:
                cache[key] = self._month(year, month)
            return cache[key]

        for year, month in _months(since, until):
            # 日历视图可能带上相邻月份的补位日，只认当月的，跨月时才不会重复
            prefix = f"{year:04d}-{month:02d}-"
            for raw in month_rows(year, month):
                day = _day_of(raw)
                orders = [o for o in raw.get("orders") or [] if isinstance(o, dict)]
                if day and day.startswith(prefix) and low <= day <= high and orders:
                    if day in days:
                        raise EomError("EOM 排期包含重复日期，请刷新后重试")
                    days[day] = orders
        # 工单可能在选定区间外已填日报；按开始时间补齐历史月份，不扩展待填集合。
        starts = [
            start for orders in days.values() for order in orders
            if (start := _day_of({"time": order.get("ewlStartTime")}))
            and start <= now.isoformat()
        ]
        if starts:
            for year, month in _months(date.fromisoformat(min(starts)), now):
                month_rows(year, month)
        return days, cache

    def commit_report(self, rows: list[dict[str, Any]]) -> None:
        """单次发送；连接错误或服务端错误均交由上层核实，禁止自动重试。"""
        response = request(
            "POST", f"{self._cfg.base_url}/core/busi/exec",
            headers={"token": self._cfg.token, "application": "platform", "tenant": "default"},
            json_body={"dir": "manHourCenter", "menuId": "ScheduleP", "modelId": "workOrders",
                       "buttonId": "increase", "datasetId": "2",
                       "condition": {"op": "commitReport", "rows": rows}},
            source="EOM", attempts=1, allow_redirects=False,
        )
        if response.status_code == 401:
            raise EomAuthError(_AUTH_HINT)
        if response.status_code >= 500 or response.status_code == 429:
            raise SourceError("EOM 提交结果未知，请回读核实")
        if response.status_code >= 400:
            raise EomError(f"EOM 提交失败：HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError("EOM 提交响应无法识别，请回读核实") from exc
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise SourceError("EOM 提交响应无法识别，请回读核实")
        if payload.get("code") == 401:
            raise EomAuthError(_AUTH_HINT)
        if "code" not in payload:
            raise SourceError("EOM 提交响应无法识别，请回读核实")
        if payload.get("code") != 200 or isinstance(payload.get("code"), bool):
            raise EomError("EOM 拒绝提交，请在 EOM 核对日程表")

    def _month(self, year: int, month: int) -> list[dict[str, Any]]:
        response = request(
            "POST",
            f"{self._cfg.base_url}/core/busi/exec",
            headers={"token": self._cfg.token, "application": "platform", "tenant": "default"},
            json_body={
                "dir": "manHourCenter",
                "menuId": "ScheduleP",
                "modelId": "workOrders",
                "buttonId": "search",
                "datasetId": "2",
                "condition": {"op": "schedule", "year": year, "month": month},
            },
            source="EOM",
            allow_redirects=False,
        )
        if response.status_code == 401:
            raise EomAuthError(_AUTH_HINT)
        if response.status_code >= 400:
            raise EomError(f"EOM 请求失败：HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise EomError("EOM 返回的不是 JSON，可能是 token 失效被重定向到了登录页") from exc

        code = payload.get("code", payload.get("status"))
        if code == 401:
            raise EomAuthError(_AUTH_HINT)
        if code != 200:
            raise EomError("EOM 返回错误，请在 EOM 页面核对权限与排期")

        rows = (payload.get("datas") or {}).get("2.1")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _months(since: date, until: date) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    year, month = since.year, since.month
    while (year, month) <= (until.year, until.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _day_of(raw: dict[str, Any]) -> str | None:
    """time 可能是 2026-10-8 这种不补零的写法。"""
    value = str(raw.get("time") or "")[:10]
    try:
        year, month, day = (int(part) for part in value.split("-"))
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _calendar_orders(cache: dict[tuple[int, int], list[dict]]) -> Iterator[tuple[str, dict]]:
    for (year, month), rows in cache.items():
        prefix = f"{year:04d}-{month:02d}-"
        for raw in rows:
            day = _day_of(raw)
            if not day or not day.startswith(prefix):
                continue
            for order in raw.get("orders") or []:
                if isinstance(order, dict):
                    yield day, order


def _history(cache: dict[tuple[int, int], list[dict]], today: str) -> dict[str, list[tuple]]:
    history: dict[str, list[tuple]] = {}
    for day, order in _calendar_orders(cache):
        report = order.get("report")
        if day > today or not isinstance(report, dict) or not report.get("id"):
            continue
        rate = number(report.get("ewdRate"))
        rate = float(rate) if rate is not None and 0 <= rate <= 100 else None
        memo = report.get("memo")
        history.setdefault(str(order.get("id")), []).append((day, rate, memo if isinstance(memo, str) else ""))
    return history


def _with_rate_bounds(
    days: dict[str, list[dict]], cache: dict[tuple[int, int], list[dict]], today: str
) -> dict[str, list[dict]]:
    history = _history(cache, today)
    bounded = {}
    for day, orders in sorted(days.items()):
        bounded[day] = []
        for order in orders:
            key = str(order.get("id"))
            anchors = history.get(key, [])
            earlier = [rate for anchor_day, rate, _ in anchors if anchor_day < day and rate is not None]
            later = [rate for anchor_day, rate, _ in anchors if anchor_day > day and rate is not None]
            previous = max((entry for entry in anchors if entry[0] < day), default=None, key=lambda item: item[0])
            bounded[day].append({**order, "_rateMin": max(earlier, default=None),
                                 "_rateMax": min(later, default=None),
                                 "_previousDay": previous[0] if previous else "",
                                 "_previousMemo": previous[2] if previous else "",
                                 "_continuityId": key})
    return bounded


def _order(raw: dict[str, Any]) -> Order:
    report = raw.get("report") or {}
    progress = (
        Progress(
            memo=(report.get("memo") or "").strip(),
            issue=(report.get("ewdProblem") or "").strip(),
            plan=(report.get("tomorrowPlan") or "").strip(),
            source="eom",
            rate=report.get("ewdRate"),
            hours=report.get("ewdWorkRealTimes"),
            report_id=str(report["id"]),
        )
        if isinstance(report, dict) and report.get("id")
        else None
    )
    return Order(
        id=str(raw.get("id") or ""),
        title=(raw.get("ewlTitle") or raw.get("eTaskTitle") or "").strip(),
        task_title=(raw.get("eTaskTitle") or "").strip(),
        project_name=(raw.get("eProjectName") or "").strip(),
        progress=progress,
        rate_min=raw.get("_rateMin"),
        rate_max=raw.get("_rateMax"),
        previous_day=raw.get("_previousDay", ""),
        previous_memo=raw.get("_previousMemo", ""),
        continuity_id=raw.get("_continuityId", ""),
    )
