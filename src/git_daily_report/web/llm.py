"""对话层：OpenAI 兼容接口 + 工具调用。

模型不直接改文件，只能通过 tools 里注册的这几个操作动报告，与页面按钮同源。
整理规则放在同目录的 rules.md 里，与 skills/daily-report 共用一份，改那里两边生效。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from ..config import AiConfig, Config
from ..period import resolve
from .tools import ReportSession, ToolError

MAX_ROUNDS = 6

# 与通道无关的那批规则，和 skills/daily-report 共用一份
RULES = Path(__file__).with_name("rules.md")

CHANNEL_PREAMBLE = """你是日报整理助手。用户的报告由 Gitee 工作项与 GitLab 提交自动汇总而来，
结构是「日期 → 工作点 → 明细」。

你能做的：改写措辞让它更像人话、合并或拆分工作点、把归错的提交挪到正确的工作点、
填工时、按需要导出。

报告只能通过下面这些工具改，你没有别的写入通道。动手前先 read_report 看当前状态，
不要凭上文猜。归属存疑的工作点用 move_commits 纠正。

下面是整理报告的规则，逐条照做。"""


@lru_cache(maxsize=1)
def system_prompt() -> str:
    """通道说明 + 共用规则文件，拼成给模型的系统提示。"""
    return f"{CHANNEL_PREAMBLE}\n\n{RULES.read_text(encoding='utf-8')}"


@dataclass
class Event:
    """推给页面的一帧。"""

    type: str
    data: dict[str, Any]


def tool_schemas() -> list[dict]:
    """暴露给模型的工具定义。"""
    return [
        _schema("read_report", "读取当前区间的完整报告，动手前先看这个", {}),
        _schema(
            "sync",
            "重新从 Gitee 与 GitLab 拉数并合并进报告，改过的内容会保留",
            {"since": _str("起始日期 YYYY-MM-DD，省略则用当前区间"), "until": _str("结束日期 YYYY-MM-DD")},
        ),
        _schema(
            "set_point",
            "改写一个工作点的标题、明细或工时",
            {
                "point_id": _str("工作点 id"),
                "title": _str("新标题，不改则省略"),
                "entries": {"type": "array", "items": {"type": "string"}, "description": "新的明细列表，不改则省略"},
                "hours": {"type": "number", "description": "工时，不改则省略"},
            },
            required=["point_id"],
        ),
        _schema(
            "move_commits",
            "把提交挪到另一个工作点，用于纠正归错的归属",
            {
                "point_id": _str("提交当前所在的工作点 id"),
                "shas": {"type": "array", "items": {"type": "string"}, "description": "要挪走的提交 sha"},
                "to_point_id": _str("目标工作点 id，与 to_new_title 二选一"),
                "to_new_title": _str("新建工作点的标题，与 to_point_id 二选一"),
            },
            required=["point_id", "shas"],
        ),
        _schema(
            "merge_points",
            "把同一天的多个工作点合并成一个",
            {
                "point_ids": {"type": "array", "items": {"type": "string"}, "description": "要合并的工作点 id，至少两个"},
                "title": _str("合并后的标题，省略则沿用第一个"),
            },
            required=["point_ids"],
        ),
        _schema("drop_point", "删掉一个工作点", {"point_id": _str("工作点 id")}, required=["point_id"]),
        _schema(
            "export",
            "导出文本，md 为工作点格式，memo 为可粘贴回 EOM 的三段式",
            {"style": {"type": "string", "enum": ["md", "memo"], "description": "导出格式"}},
        ),
    ]


class Chat:
    """一轮对话：与模型来回，直到它不再要求调用工具。"""

    def __init__(self, session: ReportSession, cfg: Config, client: Any | None = None) -> None:
        self._session = session
        self._cfg = cfg
        self._client = client or _build_client(cfg.ai)
        self._handlers: dict[str, Callable[[dict], Any]] = {
            "read_report": lambda _a: session.read_report(),
            "sync": self._sync,
            "set_point": lambda a: session.set_point(
                a["point_id"],
                title=a.get("title"),
                entries=a.get("entries"),
                hours=a.get("hours"),
            ).title,
            "move_commits": lambda a: session.move_commits(
                a["point_id"],
                a["shas"],
                to_point_id=a.get("to_point_id"),
                to_new_title=a.get("to_new_title"),
            ),
            "merge_points": lambda a: session.merge_points(
                a["point_ids"], title=a.get("title")
            ).title,
            "drop_point": lambda a: session.drop_point(a["point_id"]),
            "export": lambda a: session.memo() if a.get("style") == "memo" else session.markdown(),
        }

    def run(self, messages: Sequence[dict]) -> Iterator[Event]:
        history = [{"role": "system", "content": system_prompt()}, *messages]

        for _round in range(MAX_ROUNDS):
            text, tool_calls = yield from self._one_turn(history)
            if not tool_calls:
                yield Event("done", self._session.stats())
                return

            history.append(_assistant_turn(text, tool_calls))
            for call in tool_calls:
                yield Event("tool", {"name": call["name"]})
                history.append(self._invoke(call))

        yield Event("error", {"message": f"工具调用超过 {MAX_ROUNDS} 轮仍未收敛，已中止"})
        yield Event("done", self._session.stats())

    def _one_turn(self, history: list[dict]):
        """流式读一轮回复，边读边把文字推给页面。"""
        text_parts: list[str] = []
        calls: dict[int, dict] = {}

        stream = self._client.chat.completions.create(
            model=self._cfg.ai.model,
            messages=history,
            tools=tool_schemas(),
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                text_parts.append(delta.content)
                yield Event("delta", {"text": delta.content})
            for raw in getattr(delta, "tool_calls", None) or []:
                _accumulate(calls, raw)

        return "".join(text_parts), [c for _, c in sorted(calls.items())]

    def _invoke(self, call: dict) -> dict:
        try:
            args = json.loads(call["arguments"] or "{}")
            result = self._handlers[call["name"]](args)
            payload = {"ok": True, "result": result}
        except ToolError as exc:
            payload = {"ok": False, "error": str(exc)}
        except (KeyError, ValueError, TypeError) as exc:
            payload = {"ok": False, "error": f"参数不对：{exc}"}
        return {
            "role": "tool",
            "tool_call_id": call["id"],
            "content": json.dumps(payload, ensure_ascii=False, default=str),
        }

    def _sync(self, args: dict) -> str:
        if args.get("since") or args.get("until"):
            since, until = resolve(since=args.get("since"), until=args.get("until"))
            self._session.set_period(since, until)
        return self._session.sync(self._cfg, ("gitee", "gitlab"))


def _build_client(ai: AiConfig) -> Any:
    if not ai.ready:
        raise ToolError("未配置 AI_API_KEY，无法对话；采集与导出不受影响")
    from openai import OpenAI

    return OpenAI(api_key=ai.api_key, base_url=ai.base_url or None)


def _accumulate(calls: dict[int, dict], raw: Any) -> None:
    """流式的工具调用是按片段来的，按 index 拼起来。"""
    slot = calls.setdefault(raw.index, {"id": "", "name": "", "arguments": ""})
    if raw.id:
        slot["id"] = raw.id
    function = getattr(raw, "function", None)
    if function is None:
        return
    if function.name:
        slot["name"] = function.name
    if function.arguments:
        slot["arguments"] += function.arguments


def _assistant_turn(text: str, calls: Sequence[dict]) -> dict:
    return {
        "role": "assistant",
        "content": text or None,
        "tool_calls": [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": c["arguments"]},
            }
            for c in calls
        ],
    }


def _schema(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


def _str(description: str) -> dict:
    return {"type": "string", "description": description}
