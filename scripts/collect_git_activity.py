#!/usr/bin/env python3
"""Collect bounded, read-only Git evidence for an agent-written report."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone, tzinfo
import json
from pathlib import Path
import subprocess
import sys
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_LIMIT = 500
GIT_TIMEOUT_SECONDS = 30
LOG_FORMAT = "%H%x00%P%x00%aI%x00%cI%x00%s%x00%b"
FIELD_COUNT = 6
PERIODS = ("today", "yesterday", "this-week", "last-week")


class CollectionError(ValueError):
    """An invalid request or unavailable source of Git evidence."""


@dataclass(frozen=True)
class ReportPeriod:
    since: date
    until: date
    timezone: Optional[tzinfo] = None

    def bounds(self):
        start = datetime.combine(self.since, time.min)
        end = datetime.combine(self.until + timedelta(days=1), time.min)
        if self.timezone is not None:
            return start.replace(tzinfo=self.timezone), end.replace(tzinfo=self.timezone)
        return start.astimezone(), end.astimezone()


def resolve_period(*, since=None, until=None, period=None, timezone=None, today=None):
    zone = None
    if timezone:
        try:
            # UTC remains usable on systems without an IANA timezone database.
            zone = datetime_timezone.utc if timezone == "UTC" else ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise CollectionError("Unknown timezone; use an installed IANA timezone name.") from exc
    if since is not None or until is not None:
        if not since or not until or period:
            raise CollectionError("Use both --since and --until, without --period.")
        try:
            low, high = date.fromisoformat(since), date.fromisoformat(until)
            if high < low or high == date.max:
                raise ValueError
        except ValueError as exc:
            raise CollectionError("Dates must be YYYY-MM-DD, with since <= until < 9999-12-31.") from exc
        return ReportPeriod(low, high, zone)
    selected = period or "today"
    if selected not in PERIODS:
        raise CollectionError("Unknown report period.")
    current = today or datetime.now(zone).date()
    if selected in {"today", "yesterday"}:
        day = current - timedelta(days=selected == "yesterday")
        return ReportPeriod(day, day, zone)
    start = current - timedelta(days=current.weekday())
    if selected == "last-week":
        start -= timedelta(days=7)
    return ReportPeriod(start, start + timedelta(days=6), zone)


def _git(repo, *args):
    command = [
        "git", "--no-pager", "-c", "log.showSignature=false",
        "-c", "i18n.logOutputEncoding=UTF-8", "-c", "color.ui=false",
        "-C", str(repo), *args,
    ]
    try:
        return subprocess.run(
            command, capture_output=True, encoding="utf-8", errors="replace",
            timeout=GIT_TIMEOUT_SECONDS, check=False,
        )
    except FileNotFoundError as exc:
        raise CollectionError("Git is required and must be available on PATH.") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CollectionError("Git could not finish the requested read operation.") from exc


def _checked(repo, *args):
    result = _git(repo, *args)
    if result.returncode:
        # Git errors can contain repository paths or configuration; keep them out of agent output.
        raise CollectionError("Git read failed; verify the repository, revision and Git version.")
    return result.stdout


def _root(path):
    repo = Path(path).expanduser().resolve()
    if not repo.is_dir():
        raise CollectionError("Repository directory does not exist.")
    result = _git(repo, "rev-parse", "--show-toplevel")
    if result.returncode:
        raise CollectionError("Expected a non-bare local Git working tree.")
    return Path(result.stdout.strip()).resolve()


def _revision(repo, ref):
    if not isinstance(ref, str) or not ref.strip() or ref.startswith("-"):
        raise CollectionError("Revision must name a commit, branch or tag.")
    result = _git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
    if not result.returncode:
        return result.stdout.strip()
    if ref == "HEAD":
        head = _git(repo, "symbolic-ref", "--quiet", "HEAD")
        if not head.returncode:
            exists = _git(repo, "show-ref", "--verify", "--quiet", head.stdout.strip())
            if exists.returncode == 1:
                return None
    raise CollectionError("Revision does not resolve to a commit in the repository.")


def _parse_log(raw, span):
    if not raw:
        return []
    fields = raw.split("\0")
    if fields[-1] == "":
        fields.pop()
    if len(fields) % FIELD_COUNT:
        raise CollectionError("Git returned malformed commit records.")
    start, end = span.bounds()
    commits = []
    for index in range(0, len(fields), FIELD_COUNT):
        sha, parents, authored_at, committed_at, subject, body = fields[index:index + FIELD_COUNT]
        try:
            timestamp = datetime.fromisoformat(committed_at.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError
        except ValueError as exc:
            raise CollectionError("Git returned an invalid commit timestamp.") from exc
        if not start <= timestamp < end:
            continue
        commits.append({
            "sha": sha, "parents": parents.split(),
            "authoredAt": authored_at, "committedAt": timestamp.isoformat(),
            "day": timestamp.astimezone(span.timezone).date().isoformat(),
            "subject": subject, "body": body.strip(),
        })
    return sorted(commits, key=lambda item: (datetime.fromisoformat(item["committedAt"]), item["sha"]))


def _collect_repo(repo, span, ref, authors, limit):
    sha = _revision(repo, ref)
    shallow = _checked(repo, "rev-parse", "--is-shallow-repository").strip() == "true"
    warnings = []
    if shallow:
        warnings.append("Shallow clone: available history may be incomplete.")
    commits = []
    if sha is None:
        warnings.append("The current branch has no commits.")
    else:
        start, end = span.bounds()
        args = [
            "log", "--no-merges", "-z", "--format=" + LOG_FORMAT,
            "--since-as-filter=" + (start - timedelta(seconds=1)).isoformat(),
            "--until=" + (end - timedelta(seconds=1)).isoformat(),
            "--max-count=" + str(limit + 1),
        ]
        args.extend("--author=" + author for author in authors)
        commits = _parse_log(_checked(repo, *args, sha, "--"), span)
    truncated = len(commits) > limit
    if truncated:
        warnings.append("Commit limit reached: narrow the period or increase --limit before summarizing.")
        commits = commits[-limit:]
    return {
        "name": repo.name, "revision": sha, "commits": commits,
        "shallow": shallow, "truncated": truncated, "warnings": warnings,
    }


def collect_activity(repos, span, *, ref="HEAD", authors=None, limit=DEFAULT_LIMIT):
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise CollectionError("limit must be a positive integer.")
    authors = authors or []
    if any(not isinstance(author, str) or not author.strip() for author in authors):
        raise CollectionError("Author filters must be nonempty Git author patterns.")
    roots = list(dict.fromkeys(_root(repo) for repo in repos))
    if not roots:
        raise CollectionError("Provide at least one local repository.")
    results = [_collect_repo(repo, span, ref, authors, limit) for repo in roots]
    return {
        "schemaVersion": 1,
        "range": {"since": str(span.since), "until": str(span.until),
                  "timezone": str(span.timezone) if span.timezone else "local"},
        "scope": {"revision": ref, "authorFiltered": bool(authors),
                  "dateBasis": "committer", "mergeCommitsIncluded": False},
        "repositories": results,
        "commitCount": sum(len(repo["commits"]) for repo in results),
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", type=Path,
                        help="Local repository directory; repeat for multiple repositories (default: cwd)")
    parser.add_argument("--period", choices=PERIODS, help="Named date range (default: today)")
    parser.add_argument("--since", help="Inclusive first date, YYYY-MM-DD; requires --until")
    parser.add_argument("--until", help="Inclusive last date, YYYY-MM-DD; requires --since")
    parser.add_argument("--timezone", help="IANA timezone (default: system local timezone)")
    parser.add_argument("--ref", default="HEAD", help="Commit, branch or tag (default: HEAD)")
    parser.add_argument("--author", action="append", help="Git author pattern; repeat for OR matching")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help="Maximum commits per repository (default: %(default)s)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        span = resolve_period(since=args.since, until=args.until, period=args.period,
                              timezone=args.timezone)
        result = collect_activity(args.repo or [Path.cwd()], span, ref=args.ref,
                                  authors=args.author, limit=args.limit)
    except CollectionError as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 1
    # ASCII-safe JSON also works when an agent captures a non-UTF-8 Windows pipe.
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
