"""网页服务的路由、SSE 分帧与降级行为。

起真实服务打真实请求，避免只测到处理函数本身。
"""

from __future__ import annotations

import json
import threading
from datetime import date
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

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
            headers={"Content-Type": "application/json", "Origin": self._base},
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


def raw_post(api, body=b"{}", **headers):
    request = Request(
        api._base + "/api/progress", data=body,
        headers={"Content-Type": "application/json", "Origin": api._base, **headers},
    )
    try:
        with urlopen(request) as res:
            return res.status, res.read().decode()
    except HTTPError as exc:
        return exc.code, exc.read().decode()


@pytest.mark.parametrize("headers", [
    {"Origin": "https://other.test"}, {"Origin": "null"},
    {"Content-Type": "text/plain"}, {"Host": "other.test"},
    {"Origin": "http://localhost:1"},
])
def test_write_routes_reject_cross_origin_and_non_json(client, headers):
    api, _ = client
    status, _ = raw_post(api, **headers)
    assert status in (403, 415)


@pytest.mark.parametrize("body", [b"[]", b"null", b"{", b"true"])
def test_write_routes_require_a_json_object(client, body):
    api, _ = client
    status, _ = raw_post(api, body)
    assert status == 400


def test_progress_update_persists_numbers_and_rechecks_entire_day(client):
    api, session = client
    status, body = api.post("/api/progress", {
        "day": "2026-10-26", "orderId": "1", "rate": 70, "hours": 7.5,
    })
    assert status == 200
    result = json.loads(body)
    assert result["days"][0]["orders"][0]["progress"]["hours"] == 7.5
    assert session.days[0].orders[0].progress.rate == 70
    assert store.load_days(session.path)[0].orders[0].progress.hours == 7.5


@pytest.mark.parametrize("overrides", [
    {"rate": True}, {"hours": "7.5"}, {"hours": float("nan")},
    {"rate": -1}, {"rate": 101}, {"hours": -1},
    {"day": []}, {"orderId": []},
])
def test_progress_update_rejects_invalid_fields(client, overrides):
    api, _ = client
    payload = {"day": "2026-10-26", "orderId": "1", "rate": 70, "hours": 7.5, **overrides}
    with pytest.raises(HTTPError) as exc:
        api.post("/api/progress", payload)
    assert exc.value.code == 400


def test_progress_update_never_edits_eom_reports(client):
    from dataclasses import replace
    api, session = client
    d = session.days[0]
    order = d.orders[0]
    session.days = [replace(d, orders=(replace(order, progress=replace(order.progress, source="eom")),))]
    store.save_days(session.path, SINCE, UNTIL, session.days)
    with pytest.raises(HTTPError) as exc:
        api.post("/api/progress", {"day": d.day, "orderId": order.id, "rate": 70, "hours": 7.5})
    assert exc.value.code == 400


def test_progress_update_turns_filled_report_rate_into_amend_and_back(client):
    from dataclasses import replace
    api, session = client
    d = session.days[0]
    order = d.orders[0]
    filled = replace(order.progress, source="eom", rate=60, hours=3, problems=(), report_id="900")
    store.save_days(session.path, SINCE, UNTIL, [replace(d, orders=(replace(order, progress=filled),))])
    api.post("/api/progress", {"day": d.day, "orderId": order.id, "rate": 40, "hours": 3})
    p = store.load_days(session.path)[0].orders[0].progress
    assert (p.source, p.rate, p.original_rate) == ("amend", 40, 60)
    with pytest.raises(HTTPError):
        api.post("/api/progress", {"day": d.day, "orderId": order.id, "rate": 40, "hours": 4})
    api.post("/api/progress", {"day": d.day, "orderId": order.id, "rate": 60, "hours": 3})
    p = store.load_days(session.path)[0].orders[0].progress
    assert (p.source, p.rate, p.original_rate) == ("eom", 60, None)


def test_submit_uses_only_eom_config_and_reloads_report(client, monkeypatch):
    api, session = client
    calls = []
    def fake_submit(self, cfg, *, retry_unknown=False):
        calls.append((cfg, retry_unknown))
        return {"days": [{"day": "2026-10-26", "status": "submitted"}]}
    monkeypatch.setattr(ReportSession, "submit", fake_submit, raising=False)
    status, body = api.post("/api/submit", {"since": SINCE.isoformat(), "until": UNTIL.isoformat()})
    assert status == 200
    assert json.loads(body)["days"][0]["status"] == "submitted"
    assert calls == [(FULL, False)]


@pytest.mark.parametrize("client", [Config(
    gitlab=GitlabConfig("", ""), eom=FULL.eom,
    ai=AiConfig("", "", "m"),
)], indirect=True)
def test_local_view_and_submit_do_not_require_ai_or_gitlab(client, monkeypatch):
    api, _ = client
    state = json.loads(api.get("/api/state")[1])
    assert state["days"]
    assert state["submitGaps"] == []
    assert any("AI_API_KEY" in gap for gap in state["gaps"])
    monkeypatch.setattr(ReportSession, "submit", lambda *args, **kwargs: {"days": []}, raising=False)
    assert api.post("/api/submit", {})[0] == 200


def test_submit_rejects_bad_retry_option(client):
    api, _ = client
    with pytest.raises(HTTPError) as exc:
        api.post("/api/submit", {"retryUnknown": "true"})
    assert exc.value.code == 400


def test_page_contains_numeric_editing_and_submit_controls(client):
    api, _ = client
    body = api.get("/")[1]
    assert 'id="submit"' in body
    assert '/api/progress' in body
    assert '估算' in body
    assert '7.5' in body


def test_numeric_save_marks_all_local_blocks_when_daily_sum_exceeds_limit(client):
    from dataclasses import replace
    api, session = client
    day = session.days[0]
    base = day.orders[0]
    first = replace(base, progress=replace(base.progress, rate=70, hours=4))
    second = replace(base, id="2", progress=replace(base.progress, source="eom", rate=50, hours=4))
    store.save_days(session.path, SINCE, UNTIL, [replace(day, orders=(first, second))])
    body = json.loads(api.post("/api/progress", {
        "day": day.day, "orderId": "1", "rate": 70, "hours": 4,
    })[1])
    result = body["days"][0]["orders"]
    assert any("7.5" in problem for problem in result[0]["progress"]["problems"])
    assert result[1]["progress"]["hours"] == 4


def test_numeric_save_uses_latest_file_instead_of_stale_session(client):
    from dataclasses import replace
    api, session = client
    day = session.days[0]
    old = day.orders[0]
    fresh = replace(old, progress=replace(old.progress, plan="核对验收结果"))
    store.save_days(session.path, SINCE, UNTIL, [replace(day, orders=(fresh,))])
    api.post("/api/progress", {"day": day.day, "orderId": "1", "rate": 70, "hours": 7.5})
    assert store.load_days(session.path)[0].orders[0].progress.plan == "核对验收结果"


def test_numeric_save_rechecks_progress_order_across_dates(client):
    from dataclasses import replace
    api, session = client
    day = session.days[0]
    old = day.orders[0]
    previous = replace(old, progress=replace(old.progress, rate=80, hours=7.5))
    following = replace(old, progress=replace(old.progress, rate=90, hours=7.5))
    store.save_days(session.path, SINCE, UNTIL, [
        replace(day, orders=(previous,)), replace(day, day="2026-10-27", orders=(following,)),
    ])
    body = json.loads(api.post("/api/progress", {
        "day": "2026-10-27", "orderId": "1", "rate": 70, "hours": 7.5,
    })[1])
    problems = body["days"][1]["orders"][0]["progress"]["problems"]
    assert any("进展" in problem or "进度" in problem for problem in problems)


def test_body_limit_and_absent_origin_are_rejected(client):
    api, _ = client
    assert raw_post(api, b" " * 65537)[0] == 400
    request = Request(api._base + "/api/progress", data=b"{}", headers={"Content-Type": "application/json"})
    with pytest.raises(HTTPError) as exc:
        urlopen(request)
    assert exc.value.code == 403


@pytest.mark.parametrize("client", [Config(
    gitlab=GitlabConfig("", ""), eom=FULL.eom, ai=AiConfig("", "", "m"),
)], indirect=True)
def test_submit_delegates_to_service_with_eom_only_and_refreshes_local_state(client, monkeypatch):
    from dataclasses import replace
    from git_daily_report import submit as service

    api, session = client
    calls = []
    def fake_submit(cfg, path, since, until, *, retry_unknown=False):
        calls.append((cfg, since, until, retry_unknown))
        day = store.load_days(path)[0]
        order = day.orders[0]
        filled = replace(order, progress=replace(order.progress, source="eom", rate=70, hours=7.5))
        store.save_days(path, since, until, [replace(day, orders=(filled,))])
        return {"days": [{"day": day.day, "status": "submitted"}]}
    monkeypatch.setattr(service, "submit_report", fake_submit)
    assert api.post("/api/submit", {"retryUnknown": True})[0] == 200
    assert calls == [(FULL.eom, SINCE, UNTIL, True)]
    assert session.days[0].orders[0].progress.source == "eom"


def test_report_state_does_not_expose_credentials(client):
    api, _ = client
    body = api.get("/api/state")[1]
    payload = json.loads(body)
    assert "token" not in payload
    assert "ai" not in payload
    assert "gitlab" not in payload
    assert FULL.eom.base_url not in body
