"""Persist write intent before sending and resolve uncertain results by reading back."""

from datetime import date, datetime

from .common import ReportError, day_string, identifier, number
from .archive import archive_report
from .eom import EomClient
from .http import ServiceError
from .rows import IDS, METADATA, TEXT_FIELDS, prepare, verify
from .revisions import validate_snapshot
from .store import load_report, lock, read_state, report_path, scope_dir, write_state
from .validation import validate_days
from .workflow import merge_days


JOURNAL_VERSION = 1


def journal_path(profile):
    return scope_dir(profile) / "submissions.json"


def _journal(profile):
    path = journal_path(profile)
    empty = {"schemaVersion": JOURNAL_VERSION, "scope": profile.scope("work-orders"), "entries": {}}
    if not path.exists():
        return empty
    journal = read_state(path)
    if (not isinstance(journal, dict) or journal.get("schemaVersion") != JOURNAL_VERSION
            or journal.get("scope") != empty["scope"] or not isinstance(journal.get("entries"), dict)):
        raise ReportError("Submission journal is invalid; verify EOM before proceeding.")
    for day, entry in journal["entries"].items():
        _check_entry(day, entry, profile)
    return journal


def _check_entry(day, entry, profile):
    if day_string(day) != day or not isinstance(entry, dict) or entry.get("status") not in {"pending", "unknown", "conflict"}:
        raise ReportError("Submission journal has invalid entries; verify EOM before proceeding.")
    rows, originals = entry.get("rows"), entry.get("originalRates")
    if not isinstance(rows, list) or not rows or not isinstance(originals, dict):
        raise ReportError("Submission journal has incomplete batches; verify EOM before proceeding.")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not set(METADATA).issubset(row):
            raise ReportError("Submission journal has incomplete metadata.")
        for key in IDS:
            identifier(row[key])
        rate, hours = number(row.get("ewdRate")), number(row.get("ewdWorkRealTimes"))
        if (row.get("ewdDate") != day or row.get("ewdWorker") != profile.eom.worker_id
                or row.get("op") not in {4, 8} or row["ewlOrderId"] in seen
                or rate is None or not 0 <= rate <= 100 or hours is None or not 0 < hours <= 24
                or any(not isinstance(row.get(key), str) for key in TEXT_FIELDS)):
            raise ReportError("Submission journal has invalid row values.")
        seen.add(row["ewlOrderId"])
        if row["op"] == 8:
            identifier(row.get("id"))
            baseline = originals.get(row["ewlOrderId"])
            if isinstance(baseline, dict):
                validate_snapshot(baseline)
                if (baseline["id"] != row["id"] or number(baseline["ewdRate"]) != rate
                        or number(baseline["ewdWorkRealTimes"]) != hours):
                    raise ReportError("Text revision journal changes protected report values.")
            else:
                original = number(baseline)
                if original is None or not 0 <= original <= 100:
                    raise ReportError("Submission journal lacks original correction values.")


def _result(day, status, message):
    return {"day": day, "status": status, "message": message}


def _pending(entry, day, fresh, retry_unknown):
    state, any_written = verify(entry["rows"], fresh.get(day, []), entry["originalRates"])
    blocked = state == "conflict" or not retry_unknown or any_written
    return state, blocked


def _send(client, rows, originals, day, today):
    rejection = None
    try:
        client.commit_report(rows)
    except ServiceError as exc:
        if exc.definitive:
            rejection = str(exc)
    except ReportError:
        # Intent is already durable; even a malformed success response needs a readback.
        pass
    try:
        live = client.schedule(day, day, today=today)
        state, any_written = verify(rows, live.get(str(day), []), originals)
        if state == "unknown" and rejection and not any_written:
            state = "failed"
        return state, live, rejection
    except ReportError:
        return "unknown", None, rejection


def submit_report(profile, span, *, client=None, dry_run=False, retry_unknown=False):
    path = report_path(profile, span)
    with lock(scope_dir(profile) / ".submit"), lock(path):
        report = load_report(profile, span)
        journal = _journal(profile)
        client = client or EomClient(profile.eom)
        today = datetime.now(span.timezone).date()
        fresh = client.schedule(span.since, span.until, today=today)
        merged = validate_days(merge_days(report["days"], fresh), profile.daily_hours)
        review_path = archive_report(profile, {**report, "days": merged}) if not dry_run else None
        results, stopped = [], False
        for day in merged:
            key = day["day"]
            if stopped:
                results.append(_result(key, "blocked", "An earlier submission still needs verification."))
                continue
            entry = journal["entries"].get(key)
            if entry:
                state, blocked = _pending(entry, key, fresh, retry_unknown)
                if state == "submitted":
                    journal["entries"].pop(key)
                    results.append(_result(key, state, "Previously submitted batch confirmed by readback."))
                    continue
                if blocked:
                    entry["status"] = state
                    results.append(_result(key, state, "Previous batch is unresolved; no write was repeated."))
                    stopped = True
                    continue
            try:
                rows, originals = prepare(day, fresh.get(key, []), profile.eom.worker_id, today)
                if entry and (rows != entry["rows"] or originals != entry["originalRates"]):
                    raise ReportError("Draft differs from the unresolved submission batch; verify it first.")
            except ReportError as exc:
                results.append(_result(key, "blocked", str(exc)))
                if entry:
                    stopped = True
                continue
            if not rows:
                results.append(_result(key, "skipped", "EOM reports already exist; no write needed."))
                continue
            if dry_run:
                results.append({**_result(key, "ready", "Preflight passed; nothing was written."), "orders": len(rows)})
                continue
            state, refreshed, rejection = _write_batch(profile, journal, client, rows, originals, key, today)
            if refreshed is not None:
                fresh[key] = refreshed.get(key, [])
            stopped = state in {"unknown", "conflict"}
            messages = {"submitted": "Submitted and verified by readback.",
                        "unknown": "Result is unknown; later dates are paused.",
                        "conflict": "Saved report differs from the submitted batch; later dates are paused.",
                        "failed": rejection or "EOM rejected this batch."}
            results.append(_result(key, state, messages[state]))
        if not dry_run:
            write_state(journal_path(profile), journal)
            report["days"] = validate_days(merge_days(report["days"], fresh), profile.daily_hours)
            report["dailyHours"] = profile.daily_hours
            write_state(path, report)
        return {"days": results, **({"reviewPath": review_path} if review_path else {})}


def _write_batch(profile, journal, client, rows, originals, day, today):
    journal["entries"][day] = {"status": "pending", "rows": rows, "originalRates": originals}
    write_state(journal_path(profile), journal)
    state, fresh, rejection = _send(client, rows, originals, date.fromisoformat(day), today)
    if state in {"submitted", "failed"}:
        journal["entries"].pop(day)
    else:
        journal["entries"][day]["status"] = state
    write_state(journal_path(profile), journal)
    return state, fresh, rejection
