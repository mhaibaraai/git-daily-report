"""EOM 日程表读取。

接口取自 EOM 前端「日程表」页面：所有业务请求都走 /core/busi/exec，靠 condition.op
区分操作，op=schedule 按年月返回每天排了哪些工单以及已填的进展。服务端按 menuId
校验菜单权限，前端发请求前会把它换成当前页面的菜单 ID，日程表页面是 ScheduleP。节假日与调休
休息日在日程表上没有工单，这里不另做日历判断。

只读。不调用 commitReport，工具没有任何写回 EOM 的通道。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..config import EomConfig
from ..models import Order, Progress, ScheduleDay
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
        high = min(until, today or date.today()).isoformat()
        low = since.isoformat()
        days: list[ScheduleDay] = []
        for year, month in _months(since, until):
            # 日历视图可能带上相邻月份的补位日，只认当月的，跨月时才不会重复
            prefix = f"{year:04d}-{month:02d}-"
            for raw in self._month(year, month):
                day = _day_of(raw)
                orders = tuple(_order(o) for o in raw.get("orders") or [] if isinstance(o, dict))
                if day and day.startswith(prefix) and low <= day <= high and orders:
                    days.append(ScheduleDay(day, orders))
        return sorted(days, key=lambda d: d.day)

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
            message = payload.get("msg") or payload.get("message") or f"code={code}"
            raise EomError(f"EOM 返回错误：{message}")

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


def _order(raw: dict[str, Any]) -> Order:
    report = raw.get("report") or {}
    progress = (
        Progress(
            memo=(report.get("memo") or "").strip(),
            issue=(report.get("ewdProblem") or "").strip(),
            plan=(report.get("tomorrowPlan") or "").strip(),
            source="eom",
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
    )
