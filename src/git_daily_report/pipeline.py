"""拉排期、拉提交、调 AI 的编排层。

以 EOM 排期为准：没有排期就不必再拉提交、调 AI。提交窗口向前多取几天，
用来覆盖「提交推迟」——当天的工作常常在后面几天才推上去。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from .config import Config
from .generate import generate, make_completer
from .models import ScheduleDay
from .sources.eom import EomClient
from .sources.gitlab import GitlabClient

Progress = Callable[[str], None]

# 提交推迟或提前推送的容差
COMMIT_SLACK_DAYS = 3


@dataclass
class RunResult:
    days: list[ScheduleDay] = field(default_factory=list)
    commit_count: int = 0


def run(
    cfg: Config,
    since: date,
    until: date,
    progress: Progress | None = None,
    today: date | None = None,
) -> RunResult:
    """拉取并生成区间内每个工单块的进展。"""
    report = progress or (lambda _: None)
    now = today or date.today()

    report("EOM 排期：拉取中")
    days = EomClient(cfg.eom).schedule(since, until, today=now)
    blocks = sum(len(d.orders) for d in days)
    report(f"EOM 排期：{len(days)} 天、{blocks} 个工单块")
    if not days:
        return RunResult()

    window = (since - timedelta(days=COMMIT_SLACK_DAYS), min(until + timedelta(days=COMMIT_SLACK_DAYS), now))
    report("GitLab 提交：拉取中")
    commits = GitlabClient(cfg.gitlab).list_commits(*window)
    report(f"GitLab 提交：{len({c.project for c in commits})} 个项目、{len(commits)} 条")

    generated = generate(days, commits, make_completer(cfg.ai), report)
    return RunResult(days=generated, commit_count=len(commits))


def summarize(result: RunResult) -> str:
    """给页面顶栏一句话总结。"""
    orders = [o for d in result.days for o in d.orders]
    if not orders:
        return "这个区间在 EOM 日程表上没有排工单。"
    generated = [o for o in orders if o.progress and o.progress.source == "ai"]
    flagged = sum(1 for o in generated if o.progress.problems)
    filled = len(orders) - len(generated)
    return (
        f"共 {len(result.days)} 天、{len(orders)} 个工单块：生成 {len(generated)} 块"
        f"（{flagged} 块需复核），EOM 已填 {filled} 块；参考提交 {result.commit_count} 条。"
    )
