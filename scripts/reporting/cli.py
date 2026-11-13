"""JSON command interface used by AI agents; it never invokes a model."""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from collect_git_activity import CollectionError, PERIODS, resolve_period
from .common import ReportError, read_json
from .config import MODES, load_profile
from .store import load_report
from .archive import archive_report
from .submission import submit_report
from .revisions import revise
from .workflow import amend, apply_items, export_markdown, material


SUCCESS_STATUSES = {"submitted", "skipped", "ready"}


def _options(parser, suppress=False):
    def option(*args, **kwargs):
        if suppress:
            kwargs["default"] = argparse.SUPPRESS
        parser.add_argument(*args, **kwargs)
    option("--config", type=Path, help="Local JSON settings (or GDR_CONFIG)")
    option("--profile", default="default", help="Configuration profile (default: default)")
    option("--mode", choices=MODES, default="work-orders", help="Report workflow (default: work-orders)")
    option("--period", choices=PERIODS, help="Named period (default: today)")
    option("--since", help="Inclusive first date, YYYY-MM-DD")
    option("--until", help="Inclusive last date, YYYY-MM-DD")
    option("--timezone", help="Override the profile's report timezone")


def build_parser():
    parser = argparse.ArgumentParser(description="Collect, validate and submit agent-authored work reports.")
    _options(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("material", "Collect source evidence without replacing drafts"),
                            ("apply", "Validate and merge generated JSON"),
                            ("show", "Read a saved report without credentials"),
                            ("export", "Render a saved report as Markdown"),
                            ("review", "Archive review Markdown in the configured local folder"),
                            ("amend", "Save a percentage-only correction proposal"),
                            ("revise", "Save a text-only correction proposal"),
                            ("submit", "Submit EOM reports only when explicitly requested")):
        command = commands.add_parser(name, help=help_text)
        _options(command, suppress=True)
        if name in {"apply", "amend", "revise"}:
            command.add_argument("file", type=Path, help="Agent-authored JSON input")
        if name == "material":
            command.add_argument("--discard-stale", action="store_true", help="Explicitly archive removed EOM work-order drafts")
        if name == "export":
            command.add_argument("--output", type=Path, help="Save Markdown to this .md file")
        if name == "submit":
            command.add_argument("--dry-run", action="store_true", help="Read-only live preflight")
            command.add_argument("--retry-unknown", action="store_true", help="Explicitly retry after verifying an unresolved batch")
    return parser


def _dispatch(args, profile, span):
    if args.command in {"amend", "revise", "submit"} and args.mode != "work-orders":
        raise ReportError("This operation requires work-orders mode.")
    if args.command == "material":
        if args.discard_stale and args.mode != "work-orders":
            raise ReportError("Archiving removed work orders requires work-orders mode.")
        return material(profile, span, mode=args.mode, discard_stale=args.discard_stale)
    if args.command == "apply":
        return apply_items(profile, span, read_json(args.file), mode=args.mode)
    if args.command == "amend":
        return amend(profile, span, read_json(args.file))
    if args.command == "revise":
        return revise(profile, span, read_json(args.file))
    if args.command == "submit":
        return submit_report(profile, span, dry_run=args.dry_run, retry_unknown=args.retry_unknown)
    report = load_report(profile, span, args.mode)
    if args.command == "show":
        return report
    markdown = export_markdown(report)
    if args.command == "review":
        return {"path": archive_report(profile, report), "markdown": markdown}
    if args.output:
        if args.output.suffix.lower() != ".md":
            raise ReportError("Markdown export requires a .md destination.")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(markdown, encoding="utf-8")
        return {"path": str(args.output), "markdown": markdown}
    return {"markdown": markdown}


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        profile = load_profile(args.config, args.profile)
        if args.timezone:
            profile = replace(profile, timezone=args.timezone)
        span = resolve_period(since=args.since, until=args.until, period=args.period, timezone=profile.timezone)
        result = _dispatch(args, profile, span)
    except (ReportError, CollectionError) as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("error: Local report or configuration I/O failed.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
    if result.get("failures") or result.get("missing"):
        return 1
    if args.command == "submit" and any(day["status"] not in SUCCESS_STATUSES for day in result["days"]):
        return 1
    return 0
