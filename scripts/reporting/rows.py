"""Whitelist live EOM metadata and verify the exact saved batch."""

from .common import ReportError, day_string, identifier, number
from .revisions import matches


METADATA = {
    "ewdTitle": "ewlTitle", "ewdWorkPlanTimes": "ewlWorkPlanTimes",
    "ewdWorker": "ewlWorker", "ewdWorkerName": "ewlWorkerName", "ewdDept": "ewlDept",
    "ewdNum": "ewlNum", "ewdStartTime": "ewlStartTime", "ewdEndTime": "ewlEndTime",
    "ewlOrderId": "id", "eProjectId": "eProjectId", "eTaskId": "eTaskId",
    "eProjectName": "eProjectName", "eTaskTitle": "eTaskTitle", "type": "type",
}
IDS = {"ewdWorker", "ewdDept", "ewlOrderId", "eProjectId", "eTaskId"}
TEXT_FIELDS = ("memo", "ewdProblem", "tomorrowPlan")


def metadata(raw, worker_id):
    result = {}
    for target, source in METADATA.items():
        value = raw.get(source)
        if target in IDS:
            value = identifier(value)
        elif target == "ewdWorkPlanTimes":
            if number(value) is None or number(value) < 0:
                raise ReportError("EOM planned hours are missing or invalid.")
        elif target == "type":
            if type(value) is not int or value not in {1, 2}:
                raise ReportError("EOM work-order type is missing or invalid.")
        elif not isinstance(value, str) or not value.strip():
            raise ReportError("Required EOM metadata is missing or invalid.")
        result[target] = value
    if result["ewdWorker"] != worker_id:
        raise ReportError("EOM worker identity does not match this configuration.")
    return result


def prepare(day, live_orders, worker_id, today):
    if day_string(day["day"]) > str(today):
        raise ReportError("Future dates cannot be submitted.")
    if not live_orders:
        raise ReportError("This date no longer has a live schedule.")
    by_id = {identifier(raw.get("id")): raw for raw in live_orders}
    if len(by_id) != len(live_orders) or set(by_id) != {order["id"] for order in day["orders"]}:
        raise ReportError("The work-order schedule changed; refresh before submitting.")
    rows, originals = [], {}
    for order in day["orders"]:
        progress = order.get("progress")
        if not progress or order.get("stale"):
            raise ReportError("A scheduled work order is missing a validated report.")
        if progress.get("problems"):
            raise ReportError("This date has report validation errors: " + "; ".join(progress["problems"]))
        if progress["source"] == "eom":
            continue
        raw = by_id[order["id"]]
        row = metadata(raw, worker_id)
        if progress["source"] == "revise":
            saved = raw.get("report") or {}
            if not matches(saved, progress["originalReport"]):
                raise ReportError("The original EOM report changed; propose a new text revision.")
            row.update(memo=progress["memo"], ewdProblem=progress["issue"], tomorrowPlan=progress["plan"],
                       id=progress["reportId"], op=8, ewdWorkRealTimes=saved["ewdWorkRealTimes"])
            originals[order["id"]] = progress["originalReport"]
        elif progress["source"] == "amend":
            saved = raw.get("report") or {}
            if (identifier(saved.get("id")) != progress.get("reportId")
                    or number(saved.get("ewdRate")) != number(progress.get("originalRate"))):
                raise ReportError("The original EOM percentage or report identity changed; propose a new correction.")
            if any(not isinstance(saved.get(key), str) for key in TEXT_FIELDS):
                raise ReportError("Original EOM text is missing; cannot safely correct this report.")
            row.update({key: saved[key] for key in TEXT_FIELDS})
            row.update(id=progress["reportId"], op=8, ewdWorkRealTimes=saved["ewdWorkRealTimes"])
            originals[order["id"]] = progress["originalRate"]
        else:
            row.update(memo=progress["memo"], ewdProblem=progress["issue"], tomorrowPlan=progress["plan"],
                       op=4, ewdWorkRealTimes=progress["hours"])
        row.update(ewdRate=progress["rate"], ewdDate=day["day"], status=1)
        rows.append(row)
    return rows, originals


def verify(rows, live_orders, originals):
    by_id = {str(raw.get("id")): raw.get("report") for raw in live_orders}
    complete, any_written = True, False
    for row in rows:
        saved = by_id.get(row["ewlOrderId"])
        if not isinstance(saved, dict) or not saved.get("id"):
            complete = False
            continue
        try:
            if identifier(saved.get("ewlOrderId")) != row["ewlOrderId"] or day_string(saved.get("ewdDate")) != row["ewdDate"]:
                return "conflict", True
            if row["op"] == 8 and identifier(saved["id"]) != row["id"]:
                return "conflict", True
        except ReportError:
            return "conflict", True
        original = originals.get(row["ewlOrderId"])
        if row["op"] == 8 and isinstance(original, dict) and matches(saved, original):
            complete = False
            continue
        if any(saved.get(key) != row[key] for key in TEXT_FIELDS) or number(saved.get("ewdWorkRealTimes")) != number(row["ewdWorkRealTimes"]):
            return "conflict", True
        if number(saved.get("ewdRate")) != number(row["ewdRate"]):
            if row["op"] == 8 and number(saved.get("ewdRate")) == number(originals.get(row["ewlOrderId"])):
                complete = False
                continue
            return "conflict", True
        any_written = True
    return ("submitted" if complete else "unknown"), any_written
