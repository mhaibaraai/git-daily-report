"""生成结果的规则校验。

AI 的输出不直接信任：EOM 要求三段齐全、存在问题与明日计划必填，日报里也不能
出现文件名、函数名、英文标识符这类代码化描述。这里只做能用规则判定的检查，
发现问题交回 AI 重写，而不是在这里替它改。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .render.memo import EMPTY, SECTION_KEYS, SECTION_LABELS

# 每条规则命中的都是「像代码」的片段；全大写缩写（ERP、GIS）与版本号（V1.0）放行
_CODE_PATTERNS = (
    re.compile(r"`[^`]*`?"),
    re.compile(r"\b[\w-]+\.(?:vue|tsx?|jsx?|py|java|go|json|ya?ml|md|s?css|less|html|sql|xml|sh)\b", re.I),
    re.compile(r"\b[A-Za-z_][\w.-]*/[\w./-]+"),
    re.compile(r"\b[A-Za-z_]\w*\(\)"),
    re.compile(r"\b(?:feat|fix|chore|refactor|docs|style|perf|test|build|ci|revert)\s*(?:\([^)]*\))?\s*[:：]", re.I),
    re.compile(r"\b[a-z]+[A-Z]\w*"),
    re.compile(r"\b[A-Z][a-z]+[A-Z]\w*"),
    re.compile(r"\b[A-Za-z]+_\w+"),
    re.compile(r"\b[a-z]+(?:-[a-z]+)+\b"),
    re.compile(r"\b[a-z]{3,}\b"),
)


def code_terms(text: str) -> list[str]:
    """文本里像代码的片段，按出现顺序去重。"""
    found: list[str] = []
    for pattern in _CODE_PATTERNS:
        for match in pattern.finditer(text or ""):
            term = match.group(0)
            if not any(term in f or f in term for f in found):
                found.append(term)
    return found


def fill_empty(sections: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """三段都要在场，空段写「无」。"""
    filled: dict[str, list[str]] = {}
    for key in SECTION_KEYS:
        entries = [e.strip() for e in sections.get(key) or () if e and e.strip()]
        filled[key] = entries or [EMPTY]
    return filled


def check_block(block: Mapping[str, Any]) -> list[str]:
    """一个工单块的问题清单，空表示通过。"""
    problems: list[str] = []
    task = [e for e in block.get("task") or () if e and e.strip() and e.strip() != EMPTY]
    if not task:
        problems.append(f"{SECTION_LABELS['task']}不能为空或「{EMPTY}」")
    if not str(block.get("plan") or "").strip():
        problems.append("明日计划不能为空")

    texts = [e for key in SECTION_KEYS for e in block.get(key) or ()]
    texts += [str(block.get("issue") or ""), str(block.get("plan") or "")]
    terms = code_terms("\n".join(texts))
    if terms:
        problems.append(f"含代码化描述：{'、'.join(terms)}")
    return problems
