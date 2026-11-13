"""Adapter for the schedule/commitReport protocol, independent of any deployment."""

from datetime import date

from .common import ReportError, day_string, identifier, number
from .config import token_from
from .http import HttpTransport, ServiceError


def months(since, until):
    year, month = since.year, since.month
    while (year, month) <= (until.year, until.month):
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


class EomClient:
    def __init__(self, settings, transport=None, environ=None):
        if settings is None:
            raise ReportError("Configure an EOM adapter.")
        self.settings = settings
        self.transport = transport or HttpTransport()
        self.headers = {"token": token_from(settings, environ),
                        "application": settings.protocol["application"], "tenant": settings.protocol["tenant"]}

    def _request(self, condition, read_only):
        protocol = self.settings.protocol
        body = {key: protocol[key] for key in ("dir", "menuId", "modelId", "datasetId")}
        body.update({"buttonId": "search" if read_only else "increase", "condition": condition})
        result = self.transport.request("POST", self.settings.base_url + protocol["path"],
                                        headers=self.headers, body=body, read_only=read_only).data
        if not isinstance(result, dict) or type(result.get("code")) is not int:
            raise ServiceError("EOM returned an invalid response envelope.")
        if result["code"] != 200:
            message = "EOM credentials have expired or lack permission." if result["code"] in {401, 403} else "EOM rejected the operation."
            raise ServiceError(message, definitive=True, status=result["code"])
        return result

    def _month(self, year, month):
        result = self._request({"op": "schedule", "year": year, "month": month}, True)
        data = result.get("datas")
        rows = data.get(self.settings.protocol["datasetId"] + ".1") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ServiceError("EOM schedule dataset is missing or malformed.")
        found = {}
        prefix = f"{year:04d}-{month:02d}-"
        for row in rows:
            if not isinstance(row, dict):
                raise ServiceError("EOM schedule contains an invalid day.")
            day = day_string(row.get("time"))
            if not day.startswith(prefix):
                continue
            orders = row.get("orders", [])
            if not isinstance(orders, list):
                raise ServiceError("EOM schedule contains an invalid work-order list.")
            ids = set()
            for order in orders:
                key = self._check_order(order)
                if key in ids or identifier(order.get("ewlWorker")) != self.settings.worker_id:
                    raise ServiceError("EOM schedule has duplicate orders or an unexpected worker identity.")
                ids.add(key)
            if day in found:
                raise ServiceError("EOM schedule contains duplicate dates.")
            found[day] = orders
        return found

    @staticmethod
    def _check_order(order):
        if not isinstance(order, dict):
            raise ServiceError("EOM schedule contains an invalid work order.")
        key = identifier(order.get("id"))
        for field in ("ewlTitle", "eTaskTitle", "eProjectName"):
            if order.get(field) is not None and not isinstance(order[field], str):
                raise ServiceError("EOM work-order text is malformed.")
        report = order.get("report")
        if report is not None and not isinstance(report, dict):
            raise ServiceError("EOM schedule contains an invalid saved report.")
        if report:
            identifier(report.get("id"))
            for field in ("memo", "ewdProblem", "tomorrowPlan"):
                if report.get(field) is not None and not isinstance(report[field], str):
                    raise ServiceError("EOM saved report text is malformed.")
        return key

    def schedule(self, since, until, today=None):
        now = today or date.today()
        cache = {month: self._month(*month) for month in months(since, min(until, now))}
        selected = {day: rows for rows_by_day in cache.values() for day, rows in rows_by_day.items()
                    if str(since) <= day <= str(min(until, now)) and rows}
        if not selected:
            return {}
        starts = [day_string(raw["ewlStartTime"]) for rows in selected.values() for raw in rows
                  if raw.get("ewlStartTime")]
        earliest = min([since, *(date.fromisoformat(day) for day in starts)])
        for month in months(earliest, now):
            if month not in cache:
                cache[month] = self._month(*month)
        history = {}
        for rows_by_day in cache.values():
            for day, rows in rows_by_day.items():
                for raw in rows:
                    report = raw.get("report")
                    if day <= str(now) and report and report.get("id"):
                        rate = number(report.get("ewdRate"))
                        history.setdefault(identifier(raw["id"]), []).append((day, rate, report.get("memo", "")))
        return self._bounds(selected, history)

    @staticmethod
    def _bounds(selected, history):
        result = {}
        for day, rows in sorted(selected.items()):
            result[day] = []
            for raw in rows:
                anchors = history.get(identifier(raw["id"]), [])
                before = [r for d, r, _ in anchors if d < day and r is not None and 0 <= r <= 100]
                after = [r for d, r, _ in anchors if d > day and r is not None and 0 <= r <= 100]
                previous = max((entry for entry in anchors if entry[0] < day), default=("", None, ""))
                result[day].append({**raw, "_rateMin": float(max(before)) if before else None,
                                    "_rateMax": float(min(after)) if after else None,
                                    "_previousDay": previous[0], "_previousMemo": previous[2],
                                    "_rateAnchors": [{"day": d, "rate": float(r)} for d, r, _ in anchors
                                                     if r is not None and 0 <= r <= 100]})
        return result

    def commit_report(self, rows):
        if not isinstance(rows, list) or not rows:
            raise ReportError("No EOM rows to submit.")
        dates = {day_string(row.get("ewdDate")) for row in rows}
        if len(dates) != 1:
            raise ReportError("An EOM write batch must contain a single report date.")
        self._request({"op": "commitReport", "rows": rows,
                       "reportDate": dates.pop(), "otherMatters": []}, False)


def normalize(raw):
    report = raw.get("report") or {}
    progress = None
    if report.get("id"):
        progress = {"source": "eom", "memo": report.get("memo") or "",
                    "issue": report.get("ewdProblem") or "", "plan": report.get("tomorrowPlan") or "",
                    "rate": report.get("ewdRate"), "hours": report.get("ewdWorkRealTimes"),
                    "reportId": identifier(report["id"]), "estimatedFields": []}
    return {"id": identifier(raw["id"]), "title": raw.get("ewlTitle") or "",
            "taskTitle": raw.get("eTaskTitle") or "", "projectName": raw.get("eProjectName") or "",
            "workerId": identifier(raw["ewlWorker"]), "rateMin": raw.get("_rateMin"),
            "rateMax": raw.get("_rateMax"), "previousDay": raw.get("_previousDay", ""),
            "previousMemo": raw.get("_previousMemo", ""), "rateAnchors": raw.get("_rateAnchors", []),
            "progress": progress}
