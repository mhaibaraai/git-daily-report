"""Load profiles with inline or environment credentials hidden from representations."""

from dataclasses import dataclass, field
from datetime import timezone
import os
from pathlib import Path
import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import ReportError, fingerprint, identifier, keys, number, read_json, text


MODES = ("work-orders", "gitlab")
GITLAB_FIELDS = {"base_url", "token", "token_env", "projects", "authors", "all_authors", "max_pages",
                 "merge_limit", "allow_http"}
EOM_FIELDS = {"base_url", "token", "token_env", "worker_id", "allow_http", "protocol"}
PROTOCOL_DEFAULTS = {"path": "/core/busi/exec", "application": "platform", "tenant": "default",
                     "dir": "manHourCenter", "menuId": "ScheduleP", "modelId": "workOrders",
                     "datasetId": "2"}


def _positive(value, label):
    if type(value) is not int or value < 1:
        raise ReportError(label + " must be a positive integer.")
    return value


def _base_url(value, allow_http):
    value = text(value, "base_url")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" or (parsed.scheme == "http" and allow_http))
        if not valid or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError
        parsed.port
        return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))
    except ValueError as exc:
        raise ReportError("Invalid service URL; use HTTPS without credentials, query or fragment.") from exc


def _validated_token(value):
    if not isinstance(value, str) or not value.strip() or not value.isascii() or any(
        ord(character) < 33 or ord(character) > 126 for character in value
    ):
        raise ReportError("Credential must be nonempty printable ASCII without whitespace.")
    return value


def token_from(settings, environ=None):
    if settings.token is not None:
        return _validated_token(settings.token)
    variable = _token_env(settings.token_env)
    value = (os.environ if environ is None else environ).get(variable)
    try:
        return _validated_token(value)
    except ReportError as exc:
        raise ReportError("Required credential environment variable is missing or invalid: " + variable) from exc


def _token_env(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ReportError("token_env must name an environment variable; use token for an inline credential.")
    return value


def _credentials(raw, default_env):
    if "token" in raw and "token_env" in raw:
        raise ReportError("Use either token or token_env, not both.")
    if "token" in raw:
        return {"token": _validated_token(raw["token"]), "token_env": None}
    return {"token": None, "token_env": _token_env(raw.get("token_env", default_env))}


def _boolean(value):
    if type(value) is not bool:
        raise ReportError("Expected a boolean setting.")
    return value


def projects(value):
    if not isinstance(value, list):
        raise ReportError("projects must be an array.")
    result = []
    for entry in value:
        keys(entry, {"id", "ref"}, {"id"})
        row = {"id": identifier(entry["id"])}
        if "ref" in entry:
            row["ref"] = text(entry["ref"], "ref")
        result.append(row)
    return result


@dataclass(frozen=True)
class GitlabSettings:
    base_url: str
    token_env: Optional[str] = "GITLAB_TOKEN"
    projects: list = field(default_factory=list)
    authors: list = field(default_factory=list)
    all_authors: bool = False
    max_pages: int = 20
    merge_limit: int = 50
    token: Optional[str] = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class EomSettings:
    base_url: str
    worker_id: str
    token_env: Optional[str] = "EOM_TOKEN"
    protocol: dict = field(default_factory=lambda: dict(PROTOCOL_DEFAULTS))
    token: Optional[str] = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Profile:
    name: str
    workspace: Path
    timezone: str
    daily_hours: object = None
    commit_slack_days: int = 3
    gitlab: object = None
    eom: object = None
    mappings: list = field(default_factory=list)
    archive_dir: object = None

    def scope(self, mode):
        if mode not in MODES:
            raise ReportError("Unknown report mode.")
        if mode == "work-orders":
            if self.eom is None:
                raise ReportError("Configure an EOM adapter for work-order mode.")
            return fingerprint("eom", self.eom.base_url, self.eom.worker_id)
        if self.gitlab is None:
            raise ReportError("Configure a GitLab source.")
        return fingerprint("gitlab", self.gitlab.base_url, self.name)


def _gitlab(raw):
    if raw is None:
        return None
    keys(raw, GITLAB_FIELDS, {"base_url"})
    authors = raw.get("authors", [])
    if not isinstance(authors, list) or any(not isinstance(a, str) or not a.strip() for a in authors):
        raise ReportError("authors must be an array of nonempty exact identities.")
    all_authors = _boolean(raw.get("all_authors", False))
    if authors and all_authors:
        raise ReportError("Choose authors or all_authors, not both.")
    return GitlabSettings(
        base_url=_base_url(raw["base_url"], _boolean(raw.get("allow_http", False))),
        projects=projects(raw.get("projects", [])),
        authors=[a.strip().casefold() for a in authors], all_authors=all_authors,
        max_pages=_positive(raw.get("max_pages", 20), "max_pages"),
        merge_limit=_positive(raw.get("merge_limit", 50), "merge_limit"),
        **_credentials(raw, "GITLAB_TOKEN"),
    )


def _eom(raw):
    if raw is None:
        return None
    keys(raw, EOM_FIELDS, {"base_url", "worker_id"})
    protocol = raw.get("protocol", {})
    keys(protocol, PROTOCOL_DEFAULTS)
    protocol = {**PROTOCOL_DEFAULTS, **{key: text(value, "protocol value") for key, value in protocol.items()}}
    for header in (protocol["application"], protocol["tenant"]):
        if not header.isascii() or any(ord(character) < 32 or ord(character) > 126 for character in header):
            raise ReportError("EOM protocol headers must contain printable ASCII text.")
    if not protocol["path"].startswith("/") or protocol["path"].startswith("//") or any(
        char in protocol["path"] for char in "?#"
    ):
        raise ReportError("EOM protocol path must be an absolute URL path.")
    return EomSettings(base_url=_base_url(raw["base_url"], _boolean(raw.get("allow_http", False))),
                       worker_id=identifier(raw["worker_id"]), protocol=protocol,
                       **_credentials(raw, "EOM_TOKEN"))


def _mappings(raw):
    if not isinstance(raw, list):
        raise ReportError("mappings must be an array.")
    result = []
    selectors = {"order_id", "order_title", "project_name"}
    for mapping in raw:
        keys(mapping, selectors | {"gitlab_project", "ref"}, {"gitlab_project"})
        if len(selectors & mapping.keys()) != 1:
            raise ReportError("Each mapping needs exactly one work-order selector.")
        result.append({key: text(value, "mapping") for key, value in mapping.items()})
    return result


def load_profile(path=None, name="default"):
    path = path or os.environ.get("GDR_CONFIG")
    if not path:
        raise ReportError("Provide --config or GDR_CONFIG; see references/configuration.md.")
    path = Path(path).expanduser().resolve()
    raw = read_json(path)
    keys(raw, {"profiles"}, {"profiles"})
    if not isinstance(raw["profiles"], dict) or name not in raw["profiles"]:
        raise ReportError("Requested configuration profile does not exist.")
    profile = raw["profiles"][name]
    keys(profile, {"workspace", "timezone", "daily_hours", "commit_slack_days", "gitlab", "eom", "mappings", "archive_dir"},
         {"workspace", "timezone"})
    workspace = Path(text(profile["workspace"], "workspace")).expanduser()
    workspace = (path.parent / workspace).resolve()
    skill_root = Path(__file__).resolve().parents[2]
    if workspace == skill_root or skill_root in workspace.parents or workspace in skill_root.parents:
        raise ReportError("workspace must be separate from the installed skill directory.")
    zone = text(profile["timezone"], "timezone")
    try:
        timezone.utc if zone == "UTC" else ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ReportError("Configured timezone is not available on this system.") from exc
    hours = profile.get("daily_hours")
    if hours is not None and (number(hours) is None or not 0 < number(hours) <= 24):
        raise ReportError("daily_hours must be a positive number no greater than 24.")
    slack = profile.get("commit_slack_days", 3)
    if type(slack) is not int or not 0 <= slack <= 31:
        raise ReportError("commit_slack_days must be an integer from 0 to 31.")
    from .archive import DEFAULT_ARCHIVE_FOLDER
    archive_dir = Path(text(profile.get("archive_dir", DEFAULT_ARCHIVE_FOLDER), "archive_dir")).expanduser()
    archive_dir = (path.parent / archive_dir).resolve()
    return Profile(name, workspace, zone, hours, slack, _gitlab(profile.get("gitlab")),
                   _eom(profile.get("eom")), _mappings(profile.get("mappings", [])), archive_dir)
