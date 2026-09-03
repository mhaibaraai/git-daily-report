"""把 GitLab 提交归属到 Gitee 工作项。

commit message 与分支名里不带工作项编号，可用的信号只有两个：提交所在项目的
名字，以及工作项的活跃区间。因此归属只能是推断，每条判定都带出置信度——歧义
必须显式暴露给人看，而不是悄悄挑一个。

置信度从高到低：exact（项目名唯一命中）、fuzzy（多个候选里靠标题重合选出）、
ambiguous（多个候选无法区分）、sole（项目名对不上但当天只有一个活跃工作项）、
unmatched（归不上）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping, Sequence

from ..models import Commit, WorkItem
from .matcher import normalize

EXACT = "exact"
FUZZY = "fuzzy"
AMBIGUOUS = "ambiguous"
SOLE = "sole"
UNMATCHED = "unmatched"

# 需要人工或 AI 复核的档次
NEEDS_REVIEW = frozenset({AMBIGUOUS, UNMATCHED})

# 短词包含容易误伤，与 matcher 的模糊判定保持同一门槛
MIN_OVERLAP = 4


@dataclass(frozen=True)
class Attribution:
    """一条提交的归属判定。"""

    commit: Commit
    item: WorkItem | None
    confidence: str
    candidates: tuple[str, ...] = ()


def project_forms(project: str, mapping: Mapping[str, str]) -> set[str]:
    """提交所在项目可用于比对的名字：完整路径与末段，各过一遍覆盖表。"""
    raw = {project, project.rsplit("/", 1)[-1]}
    forms = {normalize(name) for name in raw if name}
    return {mapping.get(form, form) for form in forms if form}


def attribute_day(
    items: Sequence[WorkItem],
    commits: Sequence[Commit],
    day: date,
    mapping: Mapping[str, str] | None = None,
) -> list[Attribution]:
    """判定当天每条提交归属哪个工作项，进来几条就出去几条。"""
    mapping = mapping or {}
    active = [item for item in items if item.is_active_on(day)]

    return [_attribute_one(commit, active, day, mapping) for commit in commits]


def _attribute_one(
    commit: Commit,
    active: Sequence[WorkItem],
    day: date,
    mapping: Mapping[str, str],
) -> Attribution:
    forms = project_forms(commit.project, mapping)
    candidates = [item for item in active if normalize(item.program_name) in forms]

    if len(candidates) == 1:
        return Attribution(commit, candidates[0], EXACT)

    if candidates:
        overlapping = [item for item in candidates if _titles_overlap(commit.title, item.title)]
        if len(overlapping) == 1:
            return Attribution(commit, overlapping[0], FUZZY)
        return Attribution(
            commit,
            _narrowest(candidates, day),
            AMBIGUOUS,
            tuple(item.number for item in candidates),
        )

    if len(active) == 1:
        return Attribution(commit, active[0], SOLE)

    return Attribution(commit, None, UNMATCHED)


def _titles_overlap(commit_title: str, item_title: str) -> bool:
    """提交标题与工作项标题是否互相包含，短词不算。"""
    left, right = normalize(commit_title), normalize(item_title)
    if len(right) < MIN_OVERLAP or not left:
        return False
    return right in left or (len(left) >= MIN_OVERLAP and left in right)


def _narrowest(candidates: Sequence[WorkItem], day: date) -> WorkItem:
    """无法区分时取活跃跨度最窄的——窄窗口包含当天是更强的信号。"""
    return min(candidates, key=lambda item: (item.span_days(day), item.number))
