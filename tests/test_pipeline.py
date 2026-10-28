"""采集 + 生成的编排：提交窗口外扩、无排期不调 AI。"""

from __future__ import annotations

from datetime import date

from git_daily_report import pipeline
from git_daily_report.config import AiConfig, Config, EomConfig, GitlabConfig
from git_daily_report.models import Commit, Order, ScheduleDay

CFG = Config(
    gitlab=GitlabConfig(base_url="http://gitlab.test", token="T"),
    eom=EomConfig(base_url="http://eom.test", token="T"),
    ai=AiConfig(base_url="", api_key="K", model="m"),
)


def install(monkeypatch, days, commits, seen):
    class FakeEom:
        def __init__(self, _cfg):
            pass

        def schedule(self, since, until, today=None):
            return days

    class FakeGitlab:
        def __init__(self, _cfg):
            pass

        def list_commits(self, since, until):
            seen["window"] = (since, until)
            return commits

    def fake_generate(sched, material, complete, progress=None):
        seen["generated"] = (sched, material)
        return sched

    monkeypatch.setattr(pipeline, "EomClient", FakeEom)
    monkeypatch.setattr(pipeline, "GitlabClient", FakeGitlab)
    monkeypatch.setattr(pipeline, "generate", fake_generate)
    monkeypatch.setattr(pipeline, "make_completer", lambda _ai: None)


def test_commit_window_reaches_beyond_the_range_but_not_past_today(monkeypatch):
    seen: dict = {}
    days = [ScheduleDay("2026-10-26", (Order("1", "甲", "", "", None),))]
    install(monkeypatch, days, [Commit("g/p", "s", "2026-10-27", "完成筛选", "完成筛选", "")], seen)

    lines: list[str] = []
    result = pipeline.run(CFG, date(2026, 10, 26), date(2026, 11, 1), lines.append, today=date(2026, 10, 28))

    assert seen["window"] == (date(2026, 10, 23), date(2026, 10, 28))
    assert result.days == days
    assert any("EOM" in line for line in lines)
    assert any("GitLab" in line for line in lines)


def test_no_schedule_means_no_gitlab_or_ai_call(monkeypatch):
    seen: dict = {}
    install(monkeypatch, [], [], seen)

    result = pipeline.run(CFG, date(2026, 10, 26), date(2026, 11, 1), today=date(2026, 10, 28))

    assert result.days == []
    assert "window" not in seen and "generated" not in seen


def test_summary_counts_blocks_and_flags():
    from git_daily_report.models import Progress

    ok = Progress("具体任务：甲", "无", "乙", "ai")
    bad = Progress("具体任务：甲", "无", "乙", "ai", ("含代码词：dev",))
    filled = Progress("具体任务：甲", "无", "乙", "eom")
    days = [ScheduleDay("2026-10-26", (Order("1", "甲", "", "", ok), Order("2", "乙", "", "", bad), Order("3", "丙", "", "", filled)))]

    text = pipeline.summarize(pipeline.RunResult(days=days, commit_count=4))
    assert text == "共 1 天、3 个工单块：生成 2 块（1 块需复核），EOM 已填 1 块；参考提交 4 条。"
