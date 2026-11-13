"""Atomic private state and cross-platform process locks."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

from .common import ReportError, day_string, identifier, number, read_json


SCHEMA_VERSION = 1


def scope_dir(profile, mode="work-orders"):
    return profile.workspace / profile.scope(mode)


def report_path(profile, span, mode="work-orders"):
    return scope_dir(profile, mode) / (mode + "_" + str(span.since) + "_" + str(span.until) + ".json")


def read_state(path):
    return read_json(path)


def write_state(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        encoded = json.dumps(payload, ensure_ascii=True, indent=2, allow_nan=False) + "\n"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError, TypeError) as exc:
        raise ReportError("Cannot atomically save report state; existing state was not replaced.") from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


@contextmanager
def lock(path):
    path = Path(str(path) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        handle = path.open("a+b")
    except OSError as exc:
        raise ReportError("Cannot open the report lock.") from exc
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise ReportError("Another process is using this report or submission workspace.") from exc
        yield
    finally:
        if acquired:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def validate_report(report, profile, span, mode):
    if not isinstance(report, dict) or report.get("schemaVersion") != SCHEMA_VERSION:
        raise ReportError("Unsupported or malformed report state.")
    expected_range = {"since": str(span.since), "until": str(span.until), "timezone": profile.timezone}
    if report.get("scope") != profile.scope(mode) or report.get("mode") != mode or report.get("range") != expected_range:
        raise ReportError("Report belongs to a different instance, identity, timezone or date range.")
    if not isinstance(report.get("days"), list) or not isinstance(report.get("commits"), list):
        raise ReportError("Malformed report collections.")
    if mode == "gitlab" and report["days"]:
        raise ReportError("GitLab-only reports cannot contain EOM work orders.")
    seen_days = set()
    for day in report["days"]:
        if not isinstance(day, dict) or not isinstance(day.get("orders"), list):
            raise ReportError("Malformed report day.")
        value = day_string(day.get("day"))
        if value != day.get("day") or value in seen_days or not str(span.since) <= value <= str(span.until):
            raise ReportError("Report contains duplicate, invalid or out-of-range dates.")
        seen_days.add(value)
        seen_orders = set()
        for order in day["orders"]:
            order_id = _validate_order(order, profile.eom.worker_id)
            if order_id in seen_orders:
                raise ReportError("Duplicate order in report.")
            seen_orders.add(order_id)
    return report


def _validate_order(order, worker_id):
    if not isinstance(order, dict) or order.get("workerId") != worker_id:
        raise ReportError("Malformed order or mismatched worker identity in report.")
    order_id = identifier(order.get("id"))
    if any(not isinstance(order.get(key), str) for key in ("title", "taskTitle", "projectName")):
        raise ReportError("Malformed work-order text.")
    progress = order.get("progress")
    if progress is not None:
        if not isinstance(progress, dict) or progress.get("source") not in {"eom", "agent", "amend", "revise"}:
            raise ReportError("Malformed report progress.")
        if any(not isinstance(progress.get(key), str) for key in ("memo", "issue", "plan")):
            raise ReportError("Malformed saved report text.")
        if progress["source"] in {"eom", "amend", "revise"}:
            identifier(progress.get("reportId"))
        if progress["source"] == "revise":
            from .revisions import validate_snapshot
            validate_snapshot(progress.get("originalReport"))
            if (progress["reportId"] != progress["originalReport"]["id"]
                    or number(progress.get("rate")) != number(progress["originalReport"]["ewdRate"])
                    or number(progress.get("hours")) != number(progress["originalReport"]["ewdWorkRealTimes"])):
                raise ReportError("Text revisions cannot change report identity, progress or hours.")
        if progress["source"] == "amend":
            original = number(progress.get("originalRate"))
            if original is None or not 0 <= original <= 100:
                raise ReportError("Correction is missing its original percentage.")
    anchors = order.get("rateAnchors", [])
    if not isinstance(anchors, list):
        raise ReportError("Malformed work-order history.")
    for anchor in anchors:
        if not isinstance(anchor, dict) or number(anchor.get("rate")) is None:
            raise ReportError("Malformed work-order history anchor.")
        day_string(anchor.get("day"))
    return order_id


def load_report(profile, span, mode="work-orders", *, optional=False):
    path = report_path(profile, span, mode)
    if optional and not path.exists():
        return None
    if not path.exists():
        raise ReportError("No saved report for this range; run material first.")
    return validate_report(read_state(path), profile, span, mode)
