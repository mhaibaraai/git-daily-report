"""命令行只剩一个入口：起网页。"""

from __future__ import annotations

from datetime import date

import pytest

from git_daily_report import cli
from git_daily_report.config import AiConfig, Config, EomConfig, GitlabConfig


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
