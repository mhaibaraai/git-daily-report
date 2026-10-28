"""命令行：裸 `dr` 起网页，material / apply 给 Claude Code skill 用。"""

from __future__ import annotations

import json
from datetime import date

import pytest

from git_daily_report import cli
from git_daily_report.config import AiConfig, Config, EomConfig, GitlabConfig
from git_daily_report.models import Commit, Order, Progress, ScheduleDay
from git_daily_report.store import load_days


@pytest.fixture
def served(monkeypatch):
    calls: list[dict] = []

    def fake_serve(cfg, output_dir, since, until, *, port, open_browser):
        calls.append({"since": since, "until": until, "port": port, "open": open_browser})

    monkeypatch.setattr(cli, "serve", fake_serve)
    monkeypatch.setattr(cli, "load_config", lambda: None)
    return calls


def test_bare_dr_opens_this_week(served, monkeypatch):
    monkeypatch.setattr(cli, "week_range", lambda: (date(2026, 10, 26), date(2026, 11, 1)))
    assert cli.main([]) == 0

    assert served == [
        {"since": date(2026, 10, 26), "until": date(2026, 11, 1), "port": 0, "open": True}
    ]


def test_port_and_no_open_are_passed_through(served):
    cli.main(["--port", "8080", "--no-open"])

    assert served[0]["port"] == 8080
    assert served[0]["open"] is False


@pytest.mark.parametrize("old", ["sync", "export", "web"])
def test_old_subcommands_are_gone(old, served):
    with pytest.raises(SystemExit):
        cli.main([old])


def test_missing_credentials_name_what_is_missing_and_where_to_get_it():
    gaps = Config(
        gitlab=GitlabConfig(base_url="", token=""),
        eom=EomConfig(base_url="", token=""),
        ai=AiConfig(base_url="", api_key="", model="m"),
    ).missing()

    assert any("EOM_BASE_URL" in g for g in gaps)
    assert any("EOM_TOKEN" in g and "README" in g for g in gaps)
    assert any("GITLAB_TOKEN" in g and "read_api" in g for g in gaps)
    assert any("AI_API_KEY" in g for g in gaps)


def test_ai_key_is_optional_when_claude_writes():
    cfg = Config(
        gitlab=GitlabConfig(base_url="http://g", token="T"),
        eom=EomConfig(base_url="http://e", token="T"),
        ai=AiConfig(base_url="", api_key="", model="m"),
    )

    assert cfg.missing(need_ai=False) == []
    cfg.require(need_ai=False)


WEEK = ["--since", "2026-10-26", "--until", "2026-11-01"]
FULL_CFG = Config(
    gitlab=GitlabConfig(base_url="http://g", token="T"),
    eom=EomConfig(base_url="http://e", token="T"),
    ai=AiConfig(base_url="", api_key="", model="m"),
)


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    days = [
        ScheduleDay("2026-10-26", (Order("1", "订单筛选", "任务", "项目"),)),
        ScheduleDay(
            "2026-10-27",
            (Order("2", "已填工单", "任务", "项目", Progress("具体任务：已填", "无", "联调", "eom")),),
        ),
    ]
    commits = [Commit("g/p", "s", "2026-10-26", "feat: 新增订单筛选", "feat: 新增订单筛选", "")]
    monkeypatch.setattr(cli, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: FULL_CFG)
    monkeypatch.setattr(cli, "fetch", lambda cfg, since, until, progress: (days, commits))
    return tmp_path


def test_material_saves_schedule_and_prints_rules_material_pending(workspace, capsys):
    assert cli.main(["material", *WEEK]) == 0

    out = json.loads(capsys.readouterr().out)
    assert "输出格式" in out["rules"]
    assert out["material"]["commits"][0]["title"] == "新增订单筛选"
    assert out["pending"] == [{"day": "2026-10-26", "orderId": "1"}]
    assert out["path"] == str(workspace / "2026-W44.json")
    assert len(load_days(workspace / "2026-W44.json")) == 2


def test_material_reports_missing_config(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: Config(
        gitlab=GitlabConfig(base_url="", token=""),
        eom=EomConfig(base_url="", token=""),
        ai=AiConfig(base_url="", api_key="", model="m"),
    ))

    assert cli.main(["material", *WEEK]) == 1
    assert "EOM_TOKEN" in capsys.readouterr().err


def test_apply_validates_fills_and_saves(workspace, tmp_path, capsys):
    cli.main(["material", *WEEK])
    capsys.readouterr()
    items = tmp_path / "items.json"
    block = {"day": "2026-10-26", "orderId": "1", "task": ["完成订单筛选"], "data": [],
             "problem": [], "issue": "", "plan": ""}
    items.write_text(json.dumps({"items": [block]}, ensure_ascii=False), encoding="utf-8")

    assert cli.main(["apply", str(items), *WEEK]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["passed"] == 0
    assert out["failures"] == [{"day": "2026-10-26", "orderId": "1", "problems": ["明日计划不能为空"]}]
    assert out["missing"] == []

    items.write_text(json.dumps({"items": [{**block, "plan": "联调"}]}, ensure_ascii=False), encoding="utf-8")
    cli.main(["apply", str(items), *WEEK])
    out = json.loads(capsys.readouterr().out)
    assert out["passed"] == 1 and out["failures"] == []
    saved = load_days(workspace / "2026-W44.json")
    assert saved[0].orders[0].progress.plan == "联调"
    assert saved[1].orders[0].progress.source == "eom"


def test_apply_without_material_asks_to_run_it_first(workspace, tmp_path, capsys):
    items = tmp_path / "items.json"
    items.write_text('{"items": []}', encoding="utf-8")

    assert cli.main(["apply", str(items), *WEEK]) == 1
    assert "dr material" in capsys.readouterr().err


def test_apply_reports_unreadable_items_file(workspace, tmp_path, capsys):
    cli.main(["material", *WEEK])
    capsys.readouterr()

    assert cli.main(["apply", str(tmp_path / "nope.json"), *WEEK]) == 1
    assert "nope.json" in capsys.readouterr().err
