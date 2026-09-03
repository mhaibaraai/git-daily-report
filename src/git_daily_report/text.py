"""提交文本的清洗规则。

合并提交没有信息量，conventional commits 的类型前缀是给机器看的，两者都不该
出现在给人读的日报里。抽不出内容时留空，不做任何推测填充。
"""

from __future__ import annotations

import re
from typing import Iterable

_MERGE_COMMIT = re.compile(r"^Merge (branch|remote-tracking|pull request)", re.IGNORECASE)
_COMMIT_TYPE_PREFIX = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^)]*\))?!?:\s*",
    re.IGNORECASE,
)
_TRAILING_SEPARATOR = re.compile(r"[；;，,、]\s*$")
_BULLET_PREFIX = re.compile(r"^[-*•]\s*")


def is_merge_commit(message: str) -> bool:
    """合并提交没有信息量，不进日报。"""
    return bool(_MERGE_COMMIT.match(message.strip()))


def strip_commit_prefix(message: str) -> str:
    """去掉 conventional commits 的类型前缀，只留描述。"""
    return _COMMIT_TYPE_PREFIX.sub("", message).strip()


def clean_entry(value: str) -> str:
    """一条明细的规范形态：去项目符号、去尾部分隔符、去首尾空白。"""
    text = _BULLET_PREFIX.sub("", (value or "").strip())
    return _TRAILING_SEPARATOR.sub("", text).strip()


def headline(message: str) -> str:
    """取提交信息的首行。"""
    stripped = (message or "").strip()
    return stripped.splitlines()[0] if stripped else ""


def dedupe(values: Iterable[str]) -> tuple[str, ...]:
    """按出现顺序去重，丢弃空值。"""
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return tuple(seen)


def entries_from_commits(messages: Iterable[str]) -> tuple[str, ...]:
    """把提交信息批量清洗成日报明细。"""
    return dedupe(
        clean_entry(strip_commit_prefix(head))
        for head in (headline(m) for m in messages)
        if head and not is_merge_commit(head)
    )
