"""命令行入口。

日常路径是 `dr`——不带任何参数就打开网页工作台，采集、预览、导出、对话都在
页面上完成。下面的子命令只是脚本化兜底，不需要记。
"""

from __future__ import annotations

import argparse
import sys

from .config import CONFIG_DIR, OUTPUT_DIR, ConfigError, load_config, parse_sources
from .linking.attribution import NEEDS_REVIEW
from .period import resolve
from .pipeline import collect, summarize
from .render.markdown import to_markdown
from .render.memo import to_memo
from .sources._http import SourceError
from .store import load_days, merge_days, report_path, save_days
from .web.server import serve


def _period(args: argparse.Namespace) -> tuple:
    return resolve(
        since=getattr(args, "since", None),
        until=getattr(args, "until", None),
        month=getattr(args, "month", None),
        day=getattr(args, "date", None),
    )


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = load_config()
    sources = parse_sources(args.sources)
    cfg.require(sources)

    since, until = _period(args)
    result = collect(cfg, since, until, sources, CONFIG_DIR, progress=print)

    path = report_path(OUTPUT_DIR, since, until)
    merged = merge_days(load_days(path), result.days, force=args.force)
    save_days(path, since, until, merged)

    print(summarize(result, NEEDS_REVIEW))
    print(f"报告已写入 {path}")
    if not args.force:
        print("改过的内容已保留，如需强制覆盖用 --force。")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """日常入口：起本机服务并打开浏览器，其余操作都在页面上。"""
    since, until = _period(args)
    serve(
        load_config(),
        OUTPUT_DIR,
        CONFIG_DIR,
        since,
        until,
        port=args.port,
        open_browser=not args.no_open,
    )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    since, until = _period(args)
    path = report_path(OUTPUT_DIR, since, until)
    days = load_days(path)
    if not days:
        print(f"{path} 还没有内容，先执行：dr sync", file=sys.stderr)
        return 1

    render = to_memo if args.format == "memo" else to_markdown
    target = path.with_suffix(".memo.md" if args.format == "memo" else ".md")
    target.write_text(render(days), encoding="utf-8")
    print(f"已导出 {target}")
    return 0


def _add_period_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--month", help="目标月份 YYYY-MM，默认当月")
    parser.add_argument("--date", help="目标日期 YYYY-MM-DD 或 today")
    parser.add_argument("--since", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--until", help="结束日期 YYYY-MM-DD")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dr", description="从 Gitee 工作项与 GitLab 提交汇总每日工作点"
    )
    sub = parser.add_subparsers(dest="command")

    web = sub.add_parser("web", help="打开网页工作台（不带子命令时的默认行为）")
    _add_period_args(web)
    web.add_argument("--port", type=int, default=0, help="本地端口，默认随机")
    web.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    web.set_defaults(func=cmd_web)

    sync = sub.add_parser("sync", help="拉数并生成报告")
    _add_period_args(sync)
    sync.add_argument("--sources", help="逗号分隔：gitee,gitlab，默认全部")
    sync.add_argument("--force", action="store_true", help="覆盖改过的内容")
    sync.set_defaults(func=cmd_sync)

    export = sub.add_parser("export", help="导出 Markdown")
    _add_period_args(export)
    export.add_argument(
        "--format", default="md", choices=["md", "memo"], help="md 为工作点格式，memo 为三段式"
    )
    export.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    # 不带子命令时直接开网页，日常只需要记住一个 dr
    if not raw:
        raw = ["web"]
    args = build_parser().parse_args(raw)
    try:
        return args.func(args)
    except (ConfigError, SourceError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
