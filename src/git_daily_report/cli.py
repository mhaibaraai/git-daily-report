"""命令行入口。

裸 `dr` 起本机服务并打开浏览器；`material` / `apply` 给 Codex 或 Claude Code
技能生成和校验；`amend` 按各工单 ID 重排已填百分比；`submit` 独立提交本地报告到 EOM。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import OUTPUT_DIR, ConfigError, load_config
from .amend import propose
from .generate import MISSING, apply_blocks, build_material, parse_items, system_prompt
from .period import resolve, week_range
from .pipeline import fetch
from .sources._http import SourceError
from .sources.eom import EomClient
from .store import load_days, report_path, save_days
from .submit import submit_report
from .locking import report_lock
from .web.server import serve


class CliError(RuntimeError):
    """命令执行失败，原因直接打印给调用方。"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dr", description="按 EOM 日程表与 GitLab 提交生成每周的工单进展"
    )
    parser.add_argument("--port", type=int, default=0, help="本地端口，默认随机")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")

    commands = parser.add_subparsers(dest="command")
    material = commands.add_parser("material", help="拉排期与提交，输出写进展用的规则与素材")
    _add_period(material)
    apply = commands.add_parser("apply", help="校验并保存写好的进展")
    apply.add_argument("file", type=Path, help='写好的进展，格式为 {"items": [...]}')
    _add_period(apply)
    amend = commands.add_parser("amend", help="按各工单 ID 独立重排已填日报的百分比，保存待核对")
    _add_period(amend)
    submit = commands.add_parser("submit", help="将已校验的本地报告提交到 EOM")
    _add_period(submit)
    submit.add_argument("--dry-run", action="store_true", help="读取最新排期并预检，不写入 EOM")
    submit.add_argument("--retry-unknown", action="store_true", help="核实后主动重试结果未知且未发现日报的批次")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else sys.argv[1:])
    if args.command is None:
        since, until = week_range()
        serve(load_config(), OUTPUT_DIR, since, until, port=args.port, open_browser=not args.no_open)
        return 0

    try:
        since, until = resolve(since=args.since, until=args.until)
        if args.command == "material":
            result = _material(since, until)
        elif args.command == "apply":
            result = _apply(args.file, since, until)
        elif args.command == "amend":
            result = _amend(since, until)
        else:
            cfg = load_config()
            cfg.require(need_ai=False, need_gitlab=False)
            result = submit_report(cfg.eom, report_path(OUTPUT_DIR, since, until), since, until,
                                   dry_run=args.dry_run, retry_unknown=args.retry_unknown)
    except (CliError, ConfigError, SourceError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "submit" and any(
        d["status"] not in {"submitted", "skipped", "ready"} for d in result["days"]
    ):
        return 1
    return 0


def _add_period(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--since", help="起始日期 YYYY-MM-DD，默认本周一")
    parser.add_argument("--until", help="结束日期 YYYY-MM-DD，默认本周日")


def _material(since, until) -> dict[str, Any]:
    cfg = load_config()
    cfg.require(need_ai=False)
    path = report_path(OUTPUT_DIR, since, until)
    with report_lock(path):
        days, commits = fetch(cfg, since, until, lambda line: print(line, file=sys.stderr))
        save_days(path, since, until, days)
    return {
        "path": str(path),
        "rules": system_prompt(),
        "material": build_material(days, commits),
        "pending": [
            {"day": d.day, "orderId": o.id} for d in days for o in d.orders if o.progress is None
        ],
    }


def _amend(since, until) -> dict[str, Any]:
    cfg = load_config()
    cfg.require(need_ai=False, need_gitlab=False)
    path = report_path(OUTPUT_DIR, since, until)
    with report_lock(path):
        days = propose(EomClient(cfg.eom), since, until)
        save_days(path, since, until, days)
    return {
        "path": str(path),
        "changes": [
            {"day": d.day, "orderId": o.id, "title": o.title,
             "from": o.progress.original_rate, "to": o.progress.rate}
            for d in days for o in d.orders if o.progress and o.progress.source == "amend"
        ],
        "unfilled": [{"day": d.day, "orderId": o.id} for d in days for o in d.orders if o.progress is None],
    }


def _apply(file: Path, since, until) -> dict[str, Any]:
    path = report_path(OUTPUT_DIR, since, until)
    with report_lock(path):
        return _apply_locked(file, path, since, until)


def _apply_locked(file: Path, path: Path, since, until) -> dict[str, Any]:
    days = load_days(path)
    if not days:
        raise CliError(f"{path} 里没有排期，先运行 dr material 拉取同一区间。")
    try:
        raw = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"读不了进展文件 {file}：{exc}") from exc

    filled, failures = apply_blocks(days, parse_items(raw))
    save_days(path, since, until, filled)
    generated = [o for d in filled for o in d.orders if o.progress and o.progress.source == "ai"]
    return {
        "path": str(path),
        "passed": sum(1 for o in generated if not o.progress.problems),
        "failures": [
            {"day": day, "orderId": order_id, "problems": problems}
            for (day, order_id), problems in sorted(failures.items())
            if problems != [MISSING]
        ],
        "missing": [
            {"day": day, "orderId": order_id}
            for (day, order_id), problems in sorted(failures.items())
            if problems == [MISSING]
        ],
    }
