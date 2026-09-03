"""网页服务的路由、SSE 分帧与降级行为。

起真实服务打真实请求，避免只测到处理函数本身。
"""

from __future__ import annotations

import json
import threading
from datetime import date
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

import pytest

from git_daily_report import store
from git_daily_report.config import AiConfig, Config, GiteeConfig, GitlabConfig
from git_daily_report.models import Commit, DayReport, WorkPoint
from git_daily_report.web import server as web_server
from git_daily_report.web.tools import ReportSession

FULL = Config(
    gitee=GiteeConfig(access_token="T", enterprise="E", assignee="me"),
    gitlab=GitlabConfig(base_url="http://gitlab.test", token="T"),
    ai=AiConfig(base_url="", api_key="K", model="m"),
)
BLANK = Config(
    gitee=GiteeConfig(access_token="", enterprise="E", assignee=""),
    gitlab=GitlabConfig(base_url="", token=""),
    ai=AiConfig(base_url="", api_key="", model="m"),
)


def sample_days():
    commit = Commit("g/应急平台", "s1", "2026-08-10", "feat: 图层切换", "feat: 图层切换", "")
    return [
        DayReport(
            day="2026-08-10",
            points=(
                WorkPoint(
                    id="2026-08-10/A",
                    day="2026-08-10",
                    title="态势图层重构",
                    entries=("图层切换",),
                    confidence="ambiguous",
                    item_number="A",
                    candidates=("A", "B"),
                    commits=(commit,),
                ),
            ),
        )
    ]


class Client:
    def __init__(self, port: int) -> None:
        self._base = f"http://127.0.0.1:{port}"

    def get(self, path: str) -> tuple[int, str]:
        with urlopen(f"{self._base}{path}") as res:
            return res.status, res.read().decode("utf-8")

    def headers(self, path: str):
        with urlopen(f"{self._base}{path}") as res:
            return res.headers

    def post(self, path: str, payload: dict) -> tuple[int, str]:
        request = Request(
            f"{self._base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as res:
            return res.status, res.read().decode("utf-8")

    def events(self, path: str, payload: dict) -> list[dict]:
        _status, body = self.post(path, payload)
        return [
            json.loads(frame.removeprefix("data: "))
            for frame in body.split("\n\n")
            if frame.strip()
        ]


@pytest.fixture
def client(tmp_path, request):
    cfg = getattr(request, "param", FULL)
    since, until = date(2026, 8, 1), date(2026, 8, 31)
    store.save_days(store.report_path(tmp_path, since, until), since, until, sample_days())
    session = ReportSession(tmp_path, tmp_path, since, until)

    handler = web_server._make_handler(cfg, session, threading.Lock())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    # 默认 0.5 秒轮询会让每次 shutdown 都等半秒，测试里调密
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield Client(httpd.server_port), session
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_root_serves_a_self_contained_page(client):
    api, _ = client
    status, body = api.get("/")

    assert status == 200
    assert "<title>每日工作点</title>" in body
    # 页面不拉任何外部资源，离线也能用
    assert "http://" not in body.replace("http://127.0.0.1", "")
    assert "<script" in body and "src=" not in body.split("<script")[1][:40]


def test_state_carries_range_points_and_review_count(client):
    api, _ = client
    _status, body = api.get("/api/state")
    state = json.loads(body)

    assert state["range"] == {"since": "2026-08-01", "until": "2026-08-31"}
    assert state["stats"] == {"points": 1, "review": 1, "days": 1}
    assert state["days"][0]["points"][0]["note"] == "⚠ 归属存疑：A / B"
    assert state["ai"] is True
    assert state["gaps"] == []


@pytest.mark.parametrize("client", [BLANK], indirect=True)
def test_missing_credentials_surface_on_the_page_not_only_the_terminal(client):
    api, _ = client
    state = json.loads(api.get("/api/state")[1])

    assert any("GITEE_ACCESS_TOKEN" in gap for gap in state["gaps"])
    assert state["ai"] is False


def test_shortcut_switches_the_period(client):
    api, session = client
    api.post("/api/period", {"shortcut": "last-month"})

    assert session.since.day == 1
    assert session.until > session.since


def test_bad_period_is_a_400_not_a_stack_trace(client):
    api, _ = client
    with pytest.raises(Exception) as excinfo:
        api.post("/api/period", {"since": "2026-08-10", "until": "2026-08-01"})

    assert "400" in str(excinfo.value)


def test_export_serves_both_views(client):
    api, _ = client
    _status, md = api.get("/api/export?style=md")
    _status, memo = api.get("/api/export?style=memo")

    assert md.startswith("## 2026-08-10")
    assert "⚠" not in md  # 复制出去的正文不带标记
    assert "具体任务：图层切换" in memo


def test_download_sets_a_filename(client):
    api, _ = client
    assert "attachment" in api.headers("/api/export?download=1")["Content-Disposition"]


@pytest.mark.parametrize("client", [BLANK], indirect=True)
def test_sync_without_credentials_reports_instead_of_crashing(client):
    api, _ = client
    events = api.events("/api/sync", {})

    assert events[0]["type"] == "error"
    assert "GITEE_ACCESS_TOKEN" in events[0]["message"]


def test_sync_streams_progress_then_summary_then_done(client, monkeypatch):
    api, _ = client

    def fake_sync(_self, _cfg, _sources, progress=None):
        progress("Gitee 工作项 3 条")
        progress("GitLab 2 个项目、7 条提交")
        return "共 2 个工作点，其中 0 个待确认归属。"

    monkeypatch.setattr(ReportSession, "sync", fake_sync)
    events = api.events("/api/sync", {})

    assert [e["type"] for e in events] == ["progress", "progress", "summary", "done"]
    assert events[0]["text"] == "Gitee 工作项 3 条"
    assert events[3]["stats"]["points"] == 1


def test_gitlab_unreachable_is_a_progress_line_not_a_failure(client, monkeypatch):
    api, _ = client

    def fake_sync(_self, _cfg, _sources, progress=None):
        progress("Gitee 工作项 3 条")
        progress("GitLab 不可达，已跳过")
        return "已跳过：GitLab"

    monkeypatch.setattr(ReportSession, "sync", fake_sync)
    events = api.events("/api/sync", {})

    assert not any(e["type"] == "error" for e in events)
    assert any("不可达" in e.get("text", "") for e in events)


def test_empty_chat_message_is_rejected(client):
    api, _ = client
    events = api.events("/api/chat", {"message": "  "})

    assert events[0]["type"] == "error"


@pytest.mark.parametrize("client", [BLANK], indirect=True)
def test_chat_without_api_key_explains_itself(client):
    api, _ = client
    events = api.events("/api/chat", {"message": "帮我合并"})

    assert events[0]["type"] == "error"
    assert "AI_API_KEY" in events[0]["message"]
    assert events[-1]["type"] == "done"


def test_unknown_route_is_404(client):
    api, _ = client
    with pytest.raises(Exception) as excinfo:
        api.get("/api/nope")

    assert "404" in str(excinfo.value)
