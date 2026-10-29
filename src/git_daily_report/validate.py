"""生成结果的规则校验。

AI 的输出不直接信任：EOM 要求三段齐全、存在问题与明日计划必填，日报里也不能
出现文件名、函数名、英文标识符这类代码化描述。这里只做能用规则判定的检查，
发现问题交回 AI 重写，而不是在这里替它改。
"""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .render.memo import EMPTY, SECTION_KEYS, SECTION_LABELS
from .models import Progress, ScheduleDay

DAILY_LIMIT = Decimal("7.5")


def number(value: Any) -> Decimal | None:
    """只接受 JSON 数值，拒绝布尔值、字符串及非有限数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    result = Decimal(str(value))
    return result if result.is_finite() else None


def check_numbers(rate: Any, hours: Any) -> list[str]:
    problems: list[str] = []
    r, h = number(rate), number(hours)
    if r is None or not 0 <= r <= 100:
        problems.append("工单进展必须为 0–100 的数值")
    if h is None or not 0 < h <= DAILY_LIMIT:
        problems.append("工时必须为大于 0 且不超过 7.5 的数值")
    return problems

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
    task = [e for e in _entries(block.get("task")) if e != EMPTY]
    if not task:
        problems.append(f"{SECTION_LABELS['task']}不能为空或「{EMPTY}」")
    if not str(block.get("plan") or "").strip():
        problems.append("明日计划不能为空")

    texts = [e for key in SECTION_KEYS for e in _entries(block.get(key))]
    texts += [str(block.get("issue") or ""), str(block.get("plan") or "")]
    terms = code_terms("\n".join(texts))
    if terms:
        problems.append(f"含代码化描述：{'、'.join(terms)}")
    problems.extend(check_numbers(block.get("rate"), block.get("hours")))
    return problems


def _entries(value: Any) -> list[str]:
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


def check_progress(progress: Progress) -> list[str]:
    """从保存的三段文本重新校验，编辑数值后不保留过时的错误。"""
    sections = {}
    for key, label in SECTION_LABELS.items():
        sections[key] = [line.split("：", 1)[1] for line in progress.memo.splitlines()
                         if line.startswith(label + "：")]
    problems = check_block({**sections, "issue": progress.issue, "plan": progress.plan,
                            "rate": progress.rate, "hours": progress.hours})
    if any(len(sections[key]) != 1 for key in SECTION_KEYS):
        problems.append("今日进展必须包含具体任务、关键数据、问题解决过程三段")
    terms = code_terms(progress.memo)
    if terms:
        full_text_problem = f"含代码化描述：{'、'.join(terms)}"
        if full_text_problem not in problems:
            problems.append(full_text_problem)
    return problems


def validate_days(days: Sequence[ScheduleDay]) -> list[ScheduleDay]:
    """整日累计工时、同工单跨日累计进度，错误标在相关本地块上。"""
    errors: dict[tuple[str, str], list[str]] = {}
    histories: dict[str, list[tuple[str, str, Progress]]] = {}
    for day in days:
        total = Decimal(0)
        unknown_eom = False
        for order in day.orders:
            p = order.progress
            if p is None:
                continue
            if p.source == "ai":
                errors[(day.day, order.id)] = check_progress(p)
                rate = number(p.rate)
                minimum, maximum = number(order.rate_min), number(order.rate_max)
                if rate is not None and ((minimum is not None and rate < minimum)
                                         or (maximum is not None and rate > maximum)):
                    errors[(day.day, order.id)].append("累计进展与区间外已填日报冲突，按日期不得倒退")
            elif p.source == "amend":
                # 修正值不受旧日报推出的上下界约束，只与本地前后日期比较
                errors[(day.day, order.id)] = check_numbers(p.rate, p.hours)
            hours = number(p.hours)
            if hours is not None and hours >= 0:
                total += hours
            elif p.source == "eom":
                unknown_eom = True
            if number(p.rate) is not None:
                histories.setdefault(order.id, []).append((day.day, order.id, p))
        daily = []
        if total > DAILY_LIMIT:
            daily.append(f"当日所有工单工时合计 {total}h，超过 7.5h")
        if unknown_eom:
            daily.append("EOM 已填工时缺失或非法，无法核对当日总工时")
        for order in day.orders:
            if (day.day, order.id) in errors:
                errors[(day.day, order.id)].extend(daily)
    for history in histories.values():
        history.sort(key=lambda item: item[0])
        for i, (day, order_id, p) in enumerate(history):
            if p.source not in {"ai", "amend"}:
                continue
            before = [number(prev.rate) for _, _, prev in history[:i]]
            after = [number(next_p.rate) for _, _, next_p in history[i + 1:]]
            rate = number(p.rate)
            if any(rate < prev for prev in before) or any(rate > later for later in after):
                errors[(day, order_id)].append("同一工单累计进展按日期不得倒退")
    return [replace(day, orders=tuple(
        replace(order, progress=replace(order.progress, problems=tuple(errors[(day.day, order.id)])))
        if (day.day, order.id) in errors else order for order in day.orders
    )) for day in days]
