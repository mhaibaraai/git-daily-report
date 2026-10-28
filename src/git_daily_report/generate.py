"""AI 一次生成整周的工单进展。

素材是 EOM 排期加 GitLab 提交，规则在同目录的 generate_rules.md。AI 的输出先过
validate 的规则校验，不合格的块带着原因让 AI 重写一次，仍不合格的保留原文并
标出问题，交给人在页面上判断，不阻塞其余的块。
"""

from __future__ import annotations

import json
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import AiConfig
from .models import Commit, Order, Progress, ScheduleDay
from .render.memo import EMPTY, build_memo
from .text import clean_entry, headline, is_merge_commit, strip_commit_prefix
from .validate import check_block, fill_empty

# 流式回调：kind 为 thinking（推理过程）或 content（正文），text 为至今累计的内容
OnChunk = Callable[[str, str], None]
Complete = Callable[[list[dict], OnChunk], str]
Report = Callable[[str], None]
Key = tuple[str, str]  # (日期, 工单 id)

# 推理过程每多这么多字报一次，避免逐字刷屏
THINKING_STEP = 300

RULES = Path(__file__).with_name("generate_rules.md")
MISSING = "AI 没有生成这一块"


class GenerateError(RuntimeError):
    """AI 接口调用失败，原因要能直接说给用户听。"""


@lru_cache(maxsize=1)
def system_prompt() -> str:
    return RULES.read_text(encoding="utf-8")


def make_completer(ai: AiConfig) -> Complete:
    """OpenAI 兼容接口，流式返回，要求正文是 JSON 对象。

    整周生成要跑几分钟，流式才能把推理与写作的进度实时交给页面。
    """
    from openai import OpenAI, OpenAIError

    client = OpenAI(api_key=ai.api_key, base_url=ai.base_url or None)

    def complete(messages: list[dict], on_chunk: OnChunk) -> str:
        thinking: list[str] = []
        content: list[str] = []
        try:
            stream = client.chat.completions.create(
                model=ai.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.3,
                stream=True,
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    thinking.append(reasoning)
                    on_chunk("thinking", "".join(thinking))
                if delta.content:
                    content.append(delta.content)
                    on_chunk("content", "".join(content))
        except OpenAIError as exc:
            raise GenerateError(f"AI 接口调用失败：{exc}") from exc
        return "".join(content)

    return complete


def build_material(days: Sequence[ScheduleDay], commits: Sequence[Commit]) -> dict[str, Any]:
    """喂给 AI 的素材：排期与清洗过的提交，不带 sha、链接这类无关字段。"""
    return {
        "schedule": [
            {"day": day.day, "orders": [_order_material(o) for o in day.orders]} for day in days
        ],
        "commits": [
            {"day": c.day, "project": c.project, "title": title}
            for c in sorted(commits, key=lambda c: (c.day, c.project))
            if (title := _commit_title(c))
        ],
    }


def generate(
    days: Sequence[ScheduleDay],
    commits: Sequence[Commit],
    complete: Complete,
    progress: Report | None = None,
) -> list[ScheduleDay]:
    """给每个未填的工单块生成进展，已在 EOM 填过的原样保留。"""
    report = progress or (lambda _: None)
    pending = {(d.day, o.id) for d in days for o in d.orders if o.progress is None}
    if not pending:
        report("AI 生成：所有工单块都已在 EOM 填写，无需生成")
        return list(days)

    material = build_material(days, commits)
    report(f"AI 生成：等待响应，共 {len(pending)} 块")
    blocks = _ask(complete, _user_message(material), _tracker(report, "AI 生成", len(pending)))
    failures = _failures(pending, blocks)
    if failures:
        report(f"校验：{len(failures)} 块不合格，交给 AI 重写")
        retry = _ask(
            complete, _user_message(material, failures), _tracker(report, "AI 重写", len(failures))
        )
        blocks = {**blocks, **{k: v for k, v in retry.items() if k in failures}}
        failures = _failures(pending, blocks)
    report(f"校验：{len(failures)} 块仍需复核" if failures else "校验：全部通过")

    return [
        replace(day, orders=tuple(_fill(day.day, o, blocks, failures) for o in day.orders))
        for day in days
    ]


def _order_material(order: Order) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": order.id,
        "工单标题": order.title,
        "任务标题": order.task_title,
        "项目名称": order.project_name,
    }
    if order.progress is not None:
        entry["已填写"] = order.progress.memo
    return entry


def _commit_title(commit: Commit) -> str:
    head = headline(commit.message or commit.title)
    if not head or is_merge_commit(head):
        return ""
    return clean_entry(strip_commit_prefix(head))


def _user_message(material: Mapping[str, Any], failures: Mapping[Key, list[str]] | None = None) -> str:
    payload = dict(material)
    if failures:
        payload["只重写这些"] = [
            {"day": day, "orderId": order_id, "上次的问题": problems}
            for (day, order_id), problems in sorted(failures.items())
        ]
    return json.dumps(payload, ensure_ascii=False)


def _tracker(report: Report, stage: str, total: int) -> OnChunk:
    """把流式片段折算成「思考了多少字、写出了几块」，只在数字变化时上报。"""
    seen = {"thinking": 0, "content": 0}

    def on_chunk(kind: str, text: str) -> None:
        if kind == "thinking":
            step = len(text) // THINKING_STEP
            if step > seen["thinking"]:
                seen["thinking"] = step
                report(f"{stage}：思考中，已输出 {len(text)} 字")
            return
        done = min(text.count('"orderId"'), total)
        if done > seen["content"]:
            seen["content"] = done
            report(f"{stage}：已写出 {done}/{total} 块")

    return on_chunk


def _ask(complete: Complete, user: str, on_chunk: OnChunk) -> dict[Key, dict[str, Any]]:
    """调一次 AI，解析成按（日期, 工单）索引的块；回复不成形就当什么都没给。"""
    messages = [{"role": "system", "content": system_prompt()}, {"role": "user", "content": user}]
    raw = complete(messages, on_chunk)
    try:
        items = json.loads(raw).get("items")
    except (json.JSONDecodeError, AttributeError):
        return {}
    if not isinstance(items, list):
        return {}
    return {
        (str(item.get("day")), str(item.get("orderId"))): item
        for item in items
        if isinstance(item, dict)
    }


def _failures(pending: set[Key], blocks: Mapping[Key, dict[str, Any]]) -> dict[Key, list[str]]:
    failures: dict[Key, list[str]] = {}
    for key in pending:
        problems = check_block(blocks[key]) if key in blocks else [MISSING]
        if problems:
            failures[key] = problems
    return failures


def _fill(
    day: str, order: Order, blocks: Mapping[Key, dict[str, Any]], failures: Mapping[Key, list[str]]
) -> Order:
    if order.progress is not None:
        return order
    key = (day, order.id)
    block = blocks.get(key)
    if block is None:
        return replace(order, progress=Progress("", "", "", "ai", (MISSING,)))
    progress = Progress(
        memo=build_memo(fill_empty({k: _strings(block.get(k)) for k in ("task", "data", "problem")})),
        issue=str(block.get("issue") or "").strip() or EMPTY,
        plan=str(block.get("plan") or "").strip(),
        source="ai",
        problems=tuple(failures.get(key, ())),
    )
    return replace(order, progress=progress)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value or () if isinstance(v, (str, int, float))]
