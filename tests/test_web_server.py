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
from git_daily_report.config import AiConfig, Config, EomConfig, GitlabConfig
from git_daily_report.models import Order, Progress, ScheduleDay
from git_daily_report.web import server as web_server
from git_daily_report.web.session import ReportSession

FULL = Config(
    gitlab=GitlabConfig(base_url="http://gitlab.test", token="T"),
    eom=EomConfig(base_url="http://eom.test", token="T"),
    ai=AiConfig(base_url="", api_key="K", model="m"),
)
BLANK = Config(
    gitlab=GitlabConfig(base_url="", token=""),
    eom=EomConfig(base_url="http://eom.test", token=""),
    ai=AiConfig(base_url="", api_key="", model="m"),
)
SINCE, UNTIL = date(2026, 10, 26), date(2026, 11, 1)


def sample_days():
    progress = Progress(
        memo="具体任务：完成筛选\n关键数据：无\n问题解决过程：无",
        issue="无",
        plan="联调",
        source="ai",
        problems=("含代码词：dev",),
    )
    return [ScheduleDay("2026-10-26", (Order("1", "订单列表筛选优化", "任务", "项目", progress),))]


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
    store.save_days(store.report_path(tmp_path, SINCE, UNTIL), SINCE, UNTIL, sample_days())
    session = ReportSession(tmp_path, SINCE, UNTIL)

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
    assert "<title>EOM 周报</title>" in body
    # 页面不拉任何外部资源，离线也能用
    assert "http://" not in body.replace("http://127.0.0.1", "")
    assert "<script" in body and "src=" not in body.split("<script")[1][:40]


def test_state_carries_range_days_and_progress(client):
    api, _ = client
    state = json.loads(api.get("/api/state")[1])

    assert state["range"] == {"since": "2026-10-26", "until": "2026-11-01"}
    order = state["days"][0]["orders"][0]
    assert order["title"] == "订单列表筛选优化"
    assert order["progress"]["memo"].startswith("具体任务：完成筛选")
    assert order["progress"]["problems"] == ["含代码词：dev"]
    assert state["gaps"] == []


@pytest.mark.parametrize("client", [BLANK], indirect=True)
def test_missing_credentials_surface_on_the_page(client):
    api, _ = client
    state = json.loads(api.get("/api/state")[1])

    assert any("EOM_TOKEN" in gap for gap in state["gaps"])


def test_shortcut_switches_to_last_week(client):
    api, session = client
    api.post("/api/period", {"shortcut": "last-week"})

    assert session.since.weekday() == 0
    assert (session.until - session.since).days == 6


def test_bad_period_is_a_400_not_a_stack_trace(client):
    api, _ = client
    with pytest.raises(Exception) as excinfo:
        api.post("/api/period", {"since": "2026-10-30", "until": "2026-10-26"})

    assert "400" in str(excinfo.value)


def test_export_is_the_full_week_text(client):
    api, _ = client
    _status, text = api.get("/api/export")

    assert text.startswith("## 2026-10-26\n### 订单列表筛选优化\n具体任务：完成筛选")
    assert "明日计划：联调" in text


def test_download_sets_a_filename(client):
    api, _ = client
    assert "attachment" in api.headers("/api/export?download=1")["Content-Disposition"]


@pytest.mark.parametrize("client", [BLANK], indirect=True)
def test_sync_without_credentials_reports_instead_of_crashing(client):
    api, _ = client
    events = api.events("/api/sync", {})

    assert events[0]["type"] == "error"
    assert "EOM_TOKEN" in events[0]["message"]


def test_sync_streams_progress_then_summary_then_done(client, monkeypatch):
    api, _ = client

    def fake_sync(_self, _cfg, progress=None):
        progress("EOM 排期 5 天、10 个工单块")
        progress("AI 生成中…")
        return "已生成 10 块，1 块需复核。"

    monkeypatch.setattr(ReportSession, "sync", fake_sync)
    events = api.events("/api/sync", {})

    assert [e["type"] for e in events] == ["progress", "progress", "summary", "done"]
    assert events[0]["text"] == "EOM 排期 5 天、10 个工单块"


def test_source_errors_are_streamed_as_errors(client, monkeypatch):
    api, _ = client
    from git_daily_report.sources.eom import EomAuthError

    def fake_sync(_self, _cfg, progress=None):
        raise EomAuthError("EOM token 已失效，请更新 EOM_TOKEN")

    monkeypatch.setattr(ReportSession, "sync", fake_sync)
    events = api.events("/api/sync", {})

    assert events[0] == {"type": "error", "message": "EOM token 已失效，请更新 EOM_TOKEN"}
    assert events[-1]["type"] == "done"


def test_chat_route_is_gone(client):
    api, _ = client
    with pytest.raises(Exception) as excinfo:
        api.post("/api/chat", {"message": "hi"})

    assert "404" in str(excinfo.value)
