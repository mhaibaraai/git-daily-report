"""命令行入口：`dr` 起本机服务并打开浏览器，其余操作都在页面上。"""

from __future__ import annotations

import argparse
import sys

from .config import OUTPUT_DIR, load_config
from .period import week_range
from .web.server import serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dr", description="按 EOM 日程表与 GitLab 提交生成每周的工单进展"
    )
    parser.add_argument("--port", type=int, default=0, help="本地端口，默认随机")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else sys.argv[1:])
    since, until = week_range()
    serve(load_config(), OUTPUT_DIR, since, until, port=args.port, open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
