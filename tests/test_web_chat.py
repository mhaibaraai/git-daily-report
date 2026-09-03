"""对话的工具调用循环。

流式返回里工具调用是按片段来的，参数要按 index 拼回去；工具报错要交回模型而
不是把整轮打断。这里用假客户端跑完整循环，不碰真实接口。
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from git_daily_report import store
from git_daily_report.config import AiConfig, Config, GiteeConfig, GitlabConfig
from git_daily_report.models import DayReport, WorkPoint
from git_daily_report.web.llm import MAX_ROUNDS, Chat, tool_schemas
from git_daily_report.web.tools import ReportSession

CFG = Config(
    gitee=GiteeConfig(access_token="T", enterprise="E", assignee="me"),
    gitlab=GitlabConfig(base_url="", token="T"),
    ai=AiConfig(base_url="", api_key="K", model="m"),
)


def text_chunk(value: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=value, tool_calls=None))]
    )


def call_chunk(index: int, *, call_id="", name="", arguments=""):
    call = SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[call]))]
    )


class FakeClient:
    """按预设脚本逐轮回放，并记录每轮收到的消息。"""

    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.seen: list[list[dict]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, messages, **_kwargs):
        self.seen.append(list(messages))
        return iter(self._rounds.pop(0) if self._rounds else [text_chunk("没有更多了")])


@pytest.fixture
def session(tmp_path):
    since, until = date(2026, 8, 1), date(2026, 8, 31)
    store.save_days(store.report_path(tmp_path, since, until), since, until, [
        DayReport(
            day="2026-08-10",
            points=(
                WorkPoint(
                    id="2026-08-10/A",
                    day="2026-08-10",
                    title="态势图层重构",
                    entries=("图层切换",),
                    confidence="exact",
                    item_number="A",
                    generated_title="态势图层重构",
                    generated_entries=("图层切换",),
                ),
            ),
        )
    ])
    return ReportSession(tmp_path, tmp_path, since, until)


def run(session, rounds):
    client = FakeClient(rounds)
    events = list(Chat(session, CFG, client=client).run([{"role": "user", "content": "改一下"}]))
    return events, client


def test_plain_reply_streams_text_then_done(session):
    events, _ = run(session, [[text_chunk("好"), text_chunk("的")]])

    assert [e.type for e in events] == ["delta", "delta", "done"]
    assert "".join(e.data["text"] for e in events if e.type == "delta") == "好的"
    assert events[-1].data["points"] == 1


def test_system_prompt_states_the_no_invention_boundary(session):
    _events, client = run(session, [[text_chunk("好")]])
    system = client.seen[0][0]

    assert system["role"] == "system"
    assert "不新增事实" in system["content"]
    assert "绝不编造" in system["content"]


def test_tool_call_runs_then_the_model_gets_the_result(session):
    args = json.dumps({"point_id": "2026-08-10/A", "title": "态势图层收尾"})
    events, client = run(
        session,
        [
            [call_chunk(0, call_id="c1", name="set_point", arguments=args)],
            [text_chunk("改好了")],
        ],
    )

    assert session.find_point("2026-08-10/A").title == "态势图层收尾"
    assert [e.type for e in events] == ["tool", "delta", "done"]
    assert events[0].data["name"] == "set_point"

    tool_message = client.seen[1][-1]
    assert tool_message["role"] == "tool"
    assert json.loads(tool_message["content"])["ok"] is True


def test_fragmented_arguments_are_reassembled(session):
    events, _ = run(
        session,
        [
            [
                call_chunk(0, call_id="c1", name="set_point", arguments='{"point_id":'),
                call_chunk(0, arguments='"2026-08-10/A",'),
                call_chunk(0, arguments='"title":"拼起来的"}'),
            ],
            [text_chunk("好了")],
        ],
    )

    assert session.find_point("2026-08-10/A").title == "拼起来的"
    assert [e.type for e in events] == ["tool", "delta", "done"]


def test_two_tool_calls_in_one_turn_both_run(session):
    events, _ = run(
        session,
        [
            [
                call_chunk(0, call_id="c1", name="read_report", arguments="{}"),
                call_chunk(1, call_id="c2", name="export", arguments='{"style":"md"}'),
            ],
            [text_chunk("看完了")],
        ],
    )

    assert [e.data["name"] for e in events if e.type == "tool"] == ["read_report", "export"]


def test_tool_failure_goes_back_to_the_model_instead_of_breaking_the_turn(session):
    args = json.dumps({"point_id": "2026-08-10/NOPE", "title": "x"})
    events, client = run(
        session,
        [
            [call_chunk(0, call_id="c1", name="set_point", arguments=args)],
            [text_chunk("那条不存在")],
        ],
    )

    result = json.loads(client.seen[1][-1]["content"])
    assert result["ok"] is False
    assert "找不到工作点" in result["error"]
    assert events[-1].type == "done"


def test_runaway_tool_loop_is_cut_off(session):
    forever = [[call_chunk(0, call_id="c1", name="read_report", arguments="{}")]] * (MAX_ROUNDS + 2)
    events, _ = run(session, forever)

    assert events[-2].type == "error"
    assert "超过" in events[-2].data["message"]
    assert events[-1].type == "done"


def test_every_advertised_tool_has_a_handler(session):
    chat = Chat(session, CFG, client=FakeClient([]))
    advertised = {schema["function"]["name"] for schema in tool_schemas()}

    assert advertised == set(chat._handlers)
