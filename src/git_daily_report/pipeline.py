"""取数与归并的编排层。

主干是 git 活动：Gitee 企业工作项提供任务容器，GitLab 逐条提交提供明细。
不依赖任何工单，随时可跑。GitLab 是内网实例，脱离内网时该源自动跳过。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Sequence

from .config import Config
from .linking import matcher
from .models import Commit, DayReport, WorkItem
from .report import build_days
from .sources.gitee import GiteeClient
from .sources.gitlab import GitlabClient, GitlabUnreachable

Progress = Callable[[str], None]


@dataclass
class SyncResult:
    days: list[DayReport] = field(default_factory=list)
    skipped_sources: list[str] = field(default_factory=list)
    item_count: int = 0
    commit_count: int = 0

    @property
    def point_count(self) -> int:
        return sum(len(d.points) for d in self.days)

    def review_count(self, needs_review: frozenset[str]) -> int:
        return sum(1 for d in self.days for p in d.points if p.confidence in needs_review)


def collect(
    cfg: Config,
    since: date,
    until: date,
    sources: Sequence[str],
    config_dir: Path,
    progress: Progress | None = None,
) -> SyncResult:
    """按启用的数据源拉数并归并成每日工作点。"""
    report = progress or (lambda _: None)
    result = SyncResult()

    items: list[WorkItem] = []
    if "gitee" in sources:
        items = GiteeClient(cfg.gitee).list_enterprise_issues(since, until)
        result.item_count = len(items)
        report(f"Gitee 工作项 {len(items)} 条")

    commits: list[Commit] = []
    if "gitlab" in sources:
        try:
            commits = GitlabClient(cfg.gitlab).list_commits(since, until)
            result.commit_count = len(commits)
            projects = len({c.project for c in commits})
            report(f"GitLab {projects} 个项目、{len(commits)} 条提交")
        except GitlabUnreachable as exc:
            result.skipped_sources.append(f"GitLab（{exc}）")
            report("GitLab 不可达，已跳过")

    result.days = build_days(items, commits, since, until, matcher.load_mapping(config_dir))
    return result


def summarize(result: SyncResult, needs_review: frozenset[str]) -> str:
    """给终端与页面顶栏一句话总结。"""
    review = result.review_count(needs_review)
    lines = [f"共 {result.point_count} 个工作点，其中 {review} 个待确认归属。"]
    lines.append(f"素材：工作项 {result.item_count} 条、提交 {result.commit_count} 条。")
    lines.extend(f"已跳过：{skipped}" for skipped in result.skipped_sources)
    return "\n".join(lines)
