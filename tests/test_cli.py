"""CLI 端到端：拉数、合并、导出，以及错误的退出码。"""

from __future__ import annotations

from datetime import date

import pytest

from git_daily_report import cli, pipeline, store
from git_daily_report.config import AiConfig, Config, ConfigError, GiteeConfig, GitlabConfig, parse_sources
from git_daily_report.models import Commit, WorkItem
from git_daily_report.sources._http import SourceError

CFG = Config(
    gitee=GiteeConfig(access_token="T", enterprise="acme", assignee="me"),
    gitlab=GitlabConfig(base_url="http://gitlab.test", token="T"),
    ai=AiConfig(base_url="", api_key="", model="m"),
)


def work_item(number="A", title="态势图层重构", program="应急平台"):
    return WorkItem.from_api(
        {
            "number": number,
            "title": title,
            "state": "open",
            "created_at": "2026-07-25T09:00:00+08:00",
            "updated_at": "2026-08-10T18:00:00+08:00",
            "finished_at": None,
            "program": {"name": program},
            "html_url": "",
        }
    )


@pytest.fixture
def env(monkeypatch, tmp_path):
    """把数据源换成固定素材，输出目录指到临时目录。"""
    items = [work_item()]
    commits = [Commit("g/应急平台", "s1", "2026-08-10", "feat: 完成图层切换", "feat: 完成图层切换", "")]

    class FakeGitee:
        def __init__(self, _cfg):
            pass

        def list_enterprise_issues(self, since, until):
            return items

    class FakeGitlab:
        def __init__(self, _cfg):
            pass

        def list_commits(self, since, until):
            return commits

    monkeypatch.setattr(pipeline, "GiteeClient", FakeGitee)
    monkeypatch.setattr(pipeline, "GitlabClient", FakeGitlab)
    monkeypatch.setattr(cli, "load_config", lambda: CFG)
    monkeypatch.setattr(cli, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    return tmp_path


def test_sync_writes_a_report(env, capsys):
    assert cli.main(["sync", "--month", "2026-08"]) == 0
    days = store.load_days(env / "2026-08.json")

    assert [d.day for d in days] == ["2026-08-10"]
    assert days[0].points[0].entries == ("完成图层切换",)
    assert "共 1 个工作点" in capsys.readouterr().out


def test_sync_preserves_edits_across_runs(env):
    cli.main(["sync", "--month", "2026-08"])
    path = env / "2026-08.json"

    days = store.load_days(path)
    edited = days[0].points[0].__class__(
        **{**days[0].points[0].__dict__, "title": "我改过的标题"}
    )
    store.save_days(path, date(2026, 8, 1), date(2026, 8, 31),
                    [days[0].__class__(day=days[0].day, points=(edited,))])

    cli.main(["sync", "--month", "2026-08"])
    assert store.load_days(path)[0].points[0].title == "我改过的标题"


def test_sync_force_discards_edits(env):
    cli.main(["sync", "--month", "2026-08"])
    path = env / "2026-08.json"
    days = store.load_days(path)
    edited = days[0].points[0].__class__(**{**days[0].points[0].__dict__, "title": "我改过的"})
    store.save_days(path, date(2026, 8, 1), date(2026, 8, 31),
                    [days[0].__class__(day=days[0].day, points=(edited,))])

    cli.main(["sync", "--month", "2026-08", "--force"])
    assert store.load_days(path)[0].points[0].title == "态势图层重构"


def test_export_writes_both_views(env):
    cli.main(["sync", "--month", "2026-08"])

    assert cli.main(["export", "--month", "2026-08"]) == 0
    assert (env / "2026-08.md").read_text(encoding="utf-8").startswith("## 2026-08-10")

    assert cli.main(["export", "--month", "2026-08", "--format", "memo"]) == 0
    assert "具体任务：" in (env / "2026-08.memo.md").read_text(encoding="utf-8")


def test_export_without_a_report_fails(env, capsys):
    assert cli.main(["export", "--month", "2026-08"]) == 1
    assert "先执行" in capsys.readouterr().err


def test_arbitrary_range_gets_its_own_file(env):
    cli.main(["sync", "--since", "2026-08-04", "--until", "2026-08-10"])
    assert (env / "2026-08-04_2026-08-10.json").exists()


def test_errors_exit_with_code_1(env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "collect", _raise(SourceError("Gitee 挂了")))
    assert cli.main(["sync", "--month", "2026-08"]) == 1
    assert "Gitee 挂了" in capsys.readouterr().err


def _raise(exc):
    def fail(*_args, **_kwargs):
        raise exc

    return fail


def test_gitlab_unreachable_is_reported_not_fatal(env, monkeypatch, capsys):
    class Unreachable:
        def __init__(self, _cfg):
            pass

        def list_commits(self, since, until):
            raise pipeline.GitlabUnreachable("不在内网")

    monkeypatch.setattr(pipeline, "GitlabClient", Unreachable)
    assert cli.main(["sync", "--month", "2026-08"]) == 0
    assert "GitLab 不可达，已跳过" in capsys.readouterr().out


def test_parse_sources_defaults_to_all_and_rejects_unknown():
    assert parse_sources(None) == ("gitee", "gitlab")
    assert parse_sources("gitee") == ("gitee",)
    with pytest.raises(ConfigError, match="未知数据源"):
        parse_sources("gitee,eom")


def test_missing_credentials_name_what_is_missing_and_where_to_get_it():
    blank = Config(
        gitee=GiteeConfig(access_token="", enterprise="", assignee=""),
        gitlab=GitlabConfig(base_url="", token=""),
        ai=AiConfig(base_url="", api_key="", model="m"),
    )
    gaps = blank.missing(("gitee", "gitlab"))

    assert any("GITEE_ACCESS_TOKEN" in g and "enterprises" in g for g in gaps)
    assert any("GITEE_ENTERPRISE" in g for g in gaps)
    assert any("GITEE_ASSIGNEE" in g for g in gaps)
    assert any("GITLAB_TOKEN" in g and "read_api" in g for g in gaps)


def test_there_is_no_submit_path():
    """写回 EOM 的通道已整体移除，命令面不该再出现提交入口。"""
    actions = build_actions()
    assert "submit" not in actions
    assert "login" not in actions
    assert "edit" not in actions


def build_actions() -> set[str]:
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if hasattr(a, "choices") and a.choices)
    return set(sub.choices)
