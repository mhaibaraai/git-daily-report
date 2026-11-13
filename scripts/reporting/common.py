"""Shared boundary validation and safe error messages."""

from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path


class ReportError(ValueError):
    """A user-facing error which contains no service response or credential."""


def identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ReportError("Expected a nonempty identifier.")
    return str(value).strip()


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    result = Decimal(str(value))
    return result if result.is_finite() else None


def day_string(value):
    try:
        parts = str(value).split(" ")[0].split("T")[0].split("-")
        return date(*(int(part) for part in parts)).isoformat()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReportError("Expected a valid calendar date.") from exc


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReportError("Service returned an invalid timestamp.") from exc


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ReportError("JSON contains duplicate keys.")
        result[key] = value
    return result


def parse_json(raw):
    def invalid_constant(_):
        raise ReportError("JSON contains a non-finite number.")
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise ReportError("Expected valid JSON without duplicate keys or non-finite numbers.") from exc


def read_json(path):
    if Path(path).suffix.lower() != ".json":
        raise ReportError("Configuration, input and state files must have a .json extension.")
    try:
        return parse_json(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReportError("Cannot read the requested JSON file.") from exc


def fingerprint(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode()).hexdigest()[:24]


def keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ReportError("Object has unknown or missing fields; check the documented schema.")


def text(value, label="Value"):
    if not isinstance(value, str) or not value.strip():
        raise ReportError(label + " must be nonempty text.")
    return value.strip()
