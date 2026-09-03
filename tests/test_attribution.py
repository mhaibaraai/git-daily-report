"""commit 归属到 Gitee 工作项的判定与消歧。

commit message 与分支名里不带工作项编号，只能靠「项目名归一化 + 工作项活跃
区间」推断，因此每条判定都要带出置信度，歧义必须显式暴露而不是猜一个了事。
"""

from __future__ import annotations

from datetime import date

from git_daily_report.linking.attribution import (
    AMBIGUOUS,
    EXACT,
    FUZZY,
    SOLE,
    UNMATCHED,
    attribute_day,
)
from git_daily_report.models import Commit, WorkItem


def item(number: str, title: str, program: str | None, created: str, finished: str | None = None):
    return WorkItem.from_api(
        {
            "number": number,
            "title": title,
            "state": "closed" if finished else "open",
            "created_at": f"{created}T09:00:00+08:00",
            "updated_at": f"{finished or created}T18:00:00+08:00",
            "finished_at": f"{finished}T18:00:00+08:00" if finished else None,
            "deadline": None,
            "program": {"name": program} if program else None,
            "html_url": "",
        }
    )


def commit(project: str, title: str, day: str = "2026-08-10", sha: str = "s1"):
    return Commit(project=project, sha=sha, day=day, title=title, message=title, web_url="")


DAY = date(2026, 8, 10)


def test_single_project_match_is_exact():
    items = [item("A", "态势图层重构", "应急平台", "2026-08-01")]
    result = attribute_day(items, [commit("g/应急平台", "feat: 图层切换")], DAY, {})

    assert [(a.item.number, a.confidence) for a in result] == [("A", EXACT)]


def test_last_path_segment_also_matches_the_program_name():
    items = [item("A", "任务", "web-portal", "2026-08-01")]
    result = attribute_day(items, [commit("group/team/web-portal", "feat: 甲")], DAY, {})

    assert result[0].item.number == "A"


def test_mapping_table_overrides_the_project_name():
    items = [item("A", "任务", "门户平台", "2026-08-01")]
    mapping = {"groupteamwebportal": "门户平台"}
    result = attribute_day(items, [commit("group/team/web-portal", "feat: 甲")], DAY, mapping)

    assert (result[0].item.number, result[0].confidence) == ("A", EXACT)


def test_items_outside_their_active_window_are_not_candidates():
    """8-10 的提交不该落到 8-05 就已关闭的工作项上。"""
    items = [
        item("OLD", "旧任务", "应急平台", "2026-08-01", finished="2026-08-05"),
        item("NOW", "新任务", "应急平台", "2026-08-08"),
    ]
    result = attribute_day(items, [commit("g/应急平台", "feat: 甲")], DAY, {})

    assert (result[0].item.number, result[0].confidence) == ("NOW", EXACT)


def test_title_overlap_disambiguates_multiple_candidates():
    items = [
        item("A", "态势图层重构", "应急平台", "2026-08-01"),
        item("B", "事件上报表单", "应急平台", "2026-08-01"),
    ]
    result = attribute_day(items, [commit("g/应急平台", "feat: 事件上报表单校验")], DAY, {})

    assert (result[0].item.number, result[0].confidence) == ("B", FUZZY)


def test_indistinguishable_candidates_are_flagged_not_guessed_silently():
    items = [
        item("A", "态势图层重构", "应急平台", "2026-08-01", finished="2026-08-20"),
        item("B", "事件上报表单", "应急平台", "2026-08-09", finished="2026-08-11"),
    ]
    result = attribute_day(items, [commit("g/应急平台", "chore: 调整依赖")], DAY, {})

    assert result[0].confidence == AMBIGUOUS
    assert set(result[0].candidates) == {"A", "B"}
    # 落到活跃区间中点离当天最近的那个，但仍标记为存疑
    assert result[0].item.number == "B"


def test_sole_active_item_takes_everything_when_project_name_does_not_match():
    items = [item("A", "任务", "名字对不上的项目", "2026-08-01")]
    result = attribute_day(items, [commit("g/whatever", "feat: 甲")], DAY, {})

    assert (result[0].item.number, result[0].confidence) == ("A", SOLE)


def test_unmatched_when_no_candidate_and_several_items_are_active():
    items = [
        item("A", "任务甲", "项目甲", "2026-08-01"),
        item("B", "任务乙", "项目乙", "2026-08-01"),
    ]
    result = attribute_day(items, [commit("g/项目丙", "feat: 甲")], DAY, {})

    assert result[0].item is None
    assert result[0].confidence == UNMATCHED


def test_every_commit_is_accounted_for():
    """不静默丢弃：进来几条就出去几条。"""
    items = [item("A", "任务", "项目甲", "2026-08-01"), item("B", "任务", "项目乙", "2026-08-01")]
    commits = [
        commit("g/项目甲", "feat: 甲", sha="1"),
        commit("g/项目乙", "feat: 乙", sha="2"),
        commit("g/项目丙", "feat: 丙", sha="3"),
    ]
    result = attribute_day(items, commits, DAY, {})

    assert [a.commit.sha for a in result] == ["1", "2", "3"]
