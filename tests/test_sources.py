"""Gitee 与 GitLab 客户端的分页、时间边界与错误处理。"""

from __future__ import annotations

from datetime import date

import pytest
import requests
import responses

from git_daily_report.config import GiteeConfig, GitlabConfig
from git_daily_report.sources.gitee import GiteeClient, GiteeError
from git_daily_report.sources.gitlab import GitlabClient, GitlabError

GITEE_URL = "https://gitee.com/api/v5/enterprises/acme/issues"
GITEE_CFG = GiteeConfig(access_token="T", enterprise="acme", assignee="me")
GITLAB_CFG = GitlabConfig(base_url="http://gitlab.test", token="T")


def issue(number: str, updated: str, finished: str | None = None, program: str | None = "项目甲"):
    return {
        "number": number,
        "title": f"任务{number}",
        "state": "open",
        "created_at": "2026-08-01T09:00:00+08:00",
        "updated_at": updated,
        "finished_at": finished,
        "deadline": None,
        "program": {"name": program} if program else None,
        "html_url": "",
    }


@responses.activate
def test_gitee_uses_since_and_sends_token():
    responses.add(responses.GET, GITEE_URL, json=[], headers={"total_page": "1"})
    GiteeClient(GITEE_CFG).list_enterprise_issues(date(2026, 8, 1), date(2026, 8, 31))

    request = responses.calls[0].request
    assert request.headers["Authorization"] == "token T"
    assert "since=2026-08-01T00%3A00%3A00%2B08%3A00" in request.url
    assert "assignee=me" in request.url
    # 上界不能作为请求参数，接口只认 since
    assert "created_at" not in request.url


@responses.activate
def test_gitee_paginates_by_total_page():
    responses.add(
        responses.GET, GITEE_URL, json=[issue("A", "2026-08-05T10:00:00+08:00")],
        headers={"total_page": "2"},
    )
    responses.add(
        responses.GET, GITEE_URL, json=[issue("B", "2026-08-06T10:00:00+08:00")],
        headers={"total_page": "2"},
    )
    items = GiteeClient(GITEE_CFG).list_enterprise_issues(date(2026, 8, 1), date(2026, 8, 31))
    assert [i.number for i in items] == ["A", "B"]
    assert len(responses.calls) == 2


@responses.activate
def test_gitee_filters_upper_bound_locally():
    responses.add(
        responses.GET,
        GITEE_URL,
        json=[
            issue("IN", "2026-08-31T23:59:59+08:00"),
            issue("OUT", "2026-09-01T00:00:01+08:00"),
        ],
        headers={"total_page": "1"},
    )
    items = GiteeClient(GITEE_CFG).list_enterprise_issues(date(2026, 8, 1), date(2026, 8, 31))
    assert [i.number for i in items] == ["IN"]


@responses.activate
def test_gitee_permission_error_mentions_scope():
    responses.add(responses.GET, GITEE_URL, status=403)
    with pytest.raises(GiteeError, match="enterprises"):
        GiteeClient(GITEE_CFG).list_enterprise_issues(date(2026, 8, 1), date(2026, 8, 31))


def event(day: str, project_id: int, count: int, title: str):
    return {
        "created_at": f"{day}T10:00:00.000+08:00",
        "project_id": project_id,
        "push_data": {"commit_count": count, "commit_title": title},
    }


@responses.activate
def test_gitlab_widens_event_range_because_bounds_are_exclusive():
    responses.add(responses.GET, "http://gitlab.test/api/v4/events", json=[])
    GitlabClient(GITLAB_CFG).list_commits(date(2026, 8, 1), date(2026, 8, 31))

    url = responses.calls[0].request.url
    assert "after=2026-07-31" in url
    assert "before=2026-09-01" in url


@responses.activate
def test_gitlab_invalid_token():
    responses.add(responses.GET, "http://gitlab.test/api/v4/events", status=401)
    with pytest.raises(GitlabError, match="GITLAB_TOKEN"):
        GitlabClient(GITLAB_CFG).list_commits(date(2026, 8, 1), date(2026, 8, 31))


def commit(sha: str, day: str, title: str, email: str = "me@corp.com", name: str = "我"):
    return {
        "id": sha,
        "short_id": sha[:8],
        "title": title,
        "message": f"{title}\n\n正文",
        "author_name": name,
        "author_email": email,
        "committed_date": f"{day}T10:00:00.000+08:00",
        "created_at": f"{day}T10:00:00.000+08:00",
        "web_url": f"http://gitlab.test/g/p/-/commit/{sha}",
    }


def _stub_user(email: str = "me@corp.com", name: str = "我", username: str = "me"):
    responses.add(
        responses.GET,
        "http://gitlab.test/api/v4/user",
        json={"id": 7, "username": username, "name": name, "email": email},
    )


def _stub_project(project_id: int, path: str = "g/p"):
    responses.add(
        responses.GET,
        f"http://gitlab.test/api/v4/projects/{project_id}",
        json={"path_with_namespace": path},
    )


@responses.activate
def test_commits_only_visit_projects_seen_in_push_events():
    responses.add(
        responses.GET,
        "http://gitlab.test/api/v4/events",
        json=[event("2026-08-10", 1, 3, "feat: 甲"), event("2026-08-11", 2, 1, "fix: 乙")],
    )
    _stub_user()
    for pid, path in ((1, "g/one"), (2, "g/two")):
        _stub_project(pid, path)
        responses.add(
            responses.GET,
            f"http://gitlab.test/api/v4/projects/{pid}/repository/commits",
            json=[commit(f"sha{pid}", "2026-08-10", f"feat: 项目{pid}")],
        )

    commits = GitlabClient(GITLAB_CFG).list_commits(date(2026, 8, 1), date(2026, 8, 31))

    assert [c.project for c in commits] == ["g/one", "g/two"]
    visited = [c.request.url for c in responses.calls if "repository/commits" in c.request.url]
    assert len(visited) == 2


@responses.activate
def test_commits_filter_to_the_authenticated_user():
    responses.add(responses.GET, "http://gitlab.test/api/v4/events", json=[event("2026-08-10", 1, 2, "x")])
    _stub_user()
    _stub_project(1)
    responses.add(
        responses.GET,
        "http://gitlab.test/api/v4/projects/1/repository/commits",
        json=[
            commit("mine", "2026-08-10", "feat: 我的"),
            commit("theirs", "2026-08-10", "feat: 同事的", email="other@corp.com", name="同事"),
        ],
    )

    commits = GitlabClient(GITLAB_CFG).list_commits(date(2026, 8, 1), date(2026, 8, 31))
    assert [c.sha for c in commits] == ["mine"]


@responses.activate
def test_commits_keep_everything_when_identity_is_unknown():
    """/user 拿不到可用身份时宁可多给，也不静默丢弃素材。"""
    responses.add(responses.GET, "http://gitlab.test/api/v4/events", json=[event("2026-08-10", 1, 1, "x")])
    responses.add(responses.GET, "http://gitlab.test/api/v4/user", json={"id": 7})
    _stub_project(1)
    responses.add(
        responses.GET,
        "http://gitlab.test/api/v4/projects/1/repository/commits",
        json=[commit("a", "2026-08-10", "甲"), commit("b", "2026-08-10", "乙", email="x@y.z", name="别人")],
    )

    commits = GitlabClient(GITLAB_CFG).list_commits(date(2026, 8, 1), date(2026, 8, 31))
    assert [c.sha for c in commits] == ["a", "b"]


@responses.activate
def test_commits_query_uses_day_bounds_not_the_widened_event_range():
    """commits 接口收的是 ISO8601 时间戳，不能照搬 events 那套 ±1 天外扩。"""
    responses.add(responses.GET, "http://gitlab.test/api/v4/events", json=[event("2026-08-10", 1, 1, "x")])
    _stub_user()
    _stub_project(1)
    responses.add(responses.GET, "http://gitlab.test/api/v4/projects/1/repository/commits", json=[])

    GitlabClient(GITLAB_CFG).list_commits(date(2026, 8, 1), date(2026, 8, 31))

    url = next(c.request.url for c in responses.calls if "repository/commits" in c.request.url)
    assert "since=2026-08-01T00%3A00%3A00%2B08%3A00" in url
    assert "until=2026-08-31T23%3A59%3A59%2B08%3A00" in url
    assert "all=true" in url


@responses.activate
def test_commits_paginate_via_next_page_header():
    responses.add(responses.GET, "http://gitlab.test/api/v4/events", json=[event("2026-08-10", 1, 1, "x")])
    _stub_user()
    _stub_project(1)
    responses.add(
        responses.GET,
        "http://gitlab.test/api/v4/projects/1/repository/commits",
        json=[commit("a", "2026-08-10", "甲")],
        headers={"X-Next-Page": "2"},
    )
    responses.add(
        responses.GET,
        "http://gitlab.test/api/v4/projects/1/repository/commits",
        json=[commit("b", "2026-08-11", "乙")],
    )

    commits = GitlabClient(GITLAB_CFG).list_commits(date(2026, 8, 1), date(2026, 8, 31))
    assert [c.sha for c in commits] == ["a", "b"]


@responses.activate
def test_commits_drop_days_outside_the_requested_window():
    responses.add(responses.GET, "http://gitlab.test/api/v4/events", json=[event("2026-08-10", 1, 1, "x")])
    _stub_user()
    _stub_project(1)
    responses.add(
        responses.GET,
        "http://gitlab.test/api/v4/projects/1/repository/commits",
        json=[commit("in", "2026-08-31", "甲"), commit("out", "2026-09-01", "乙")],
    )

    commits = GitlabClient(GITLAB_CFG).list_commits(date(2026, 8, 1), date(2026, 8, 31))
    assert [c.sha for c in commits] == ["in"]
    assert commits[0].day == "2026-08-31"
    assert commits[0].title == "甲"
    assert commits[0].web_url.endswith("/commit/in")


@responses.activate
def test_http_retries_on_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr("git_daily_report.sources._http.time.sleep", lambda _s: None)
    responses.add(responses.GET, "http://retry.test/x", status=503)
    responses.add(responses.GET, "http://retry.test/x", json={"ok": True})

    from git_daily_report.sources._http import request

    assert request("GET", "http://retry.test/x").json() == {"ok": True}
    assert len(responses.calls) == 2


@responses.activate
def test_http_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr("git_daily_report.sources._http.time.sleep", lambda _s: None)
    responses.add(
        responses.GET, "http://down.test/x", body=requests.ConnectionError("boom")
    )

    from git_daily_report.sources._http import SourceError, request

    with pytest.raises(SourceError, match="Gitee"):
        request("GET", "http://down.test/x", source="Gitee")


def merge_commit(sha: str, day: str, branch: str, parents: list[str]):
    return {
        "id": sha,
        "title": f"Merge branch '{branch}' into 'dev'",
        "message": f"Merge branch '{branch}' into 'dev'",
        "author_name": "我",
        "author_email": "me@corp.com",
        "committed_date": f"{day}T18:00:00.000+08:00",
        "parent_ids": parents,
        "web_url": f"http://gitlab.test/g/p/-/commit/{sha}",
    }


def _stub_compare(project_id: int, frm: str, to: str, commits: list[dict]):
    responses.add(
        responses.GET,
        f"http://gitlab.test/api/v4/projects/{project_id}/repository/compare",
        json={"commits": commits},
        match=[responses.matchers.query_param_matcher({"from": frm, "to": to}, strict_match=False)],
    )


@responses.activate
def test_merge_is_replaced_by_the_commits_it_brought_in():
    """本人的提交绝大多数是 Merge，丢掉它们等于丢掉当天的全部素材。"""
    responses.add(responses.GET, "http://gitlab.test/api/v4/events", json=[event("2026-08-10", 1, 1, "x")])
    _stub_user()
    _stub_project(1)
    responses.add(
        responses.GET,
        "http://gitlab.test/api/v4/projects/1/repository/commits",
        json=[merge_commit("m1", "2026-08-10", "dev-20260810", ["p1", "p2"])],
    )
    _stub_compare(1, "p1", "p2", [
        commit("c1", "2026-08-09", "feat: 接入地灾数据同步", email="tongshi@corp.com", name="同事"),
        commit("c2", "2026-08-09", "fix: 修复附件上传"),
    ])

    commits = GitlabClient(GITLAB_CFG).list_commits(date(2026, 8, 1), date(2026, 8, 31))

    assert [c.sha for c in commits] == ["c1", "c2"]
    assert [c.title for c in commits] == ["feat: 接入地灾数据同步", "fix: 修复附件上传"]
    # 归到合并当天——那才是本人做集成的日子
    assert {c.day for c in commits} == {"2026-08-10"}
    assert {c.project for c in commits} == {"g/p"}


@responses.activate
def test_brought_in_commits_are_kept_regardless_of_author():
    """集成别人的代码也是当天的工作，不能按作者滤掉。"""
    responses.add(responses.GET, "http://gitlab.test/api/v4/events", json=[event("2026-08-10", 1, 1, "x")])
    _stub_user()
    _stub_project(1)
    responses.add(
        responses.GET,
        "http://gitlab.test/api/v4/projects/1/repository/commits",
        json=[merge_commit("m1", "2026-08-10", "dev-x", ["p1", "p2"])],
    )
    _stub_compare(1, "p1", "p2", [commit("c1", "2026-08-09", "feat: 同事写的", email="other@corp.com", name="别人")])

    assert [c.sha for c in GitlabClient(GITLAB_CFG).list_commits(date(2026, 8, 1), date(2026, 8, 31))] == ["c1"]


@responses.activate
def test_empty_expansion_falls_back_to_the_branch_name():
    """展不开时也不能让这条提交凭空消失。"""
    responses.add(responses.GET, "http://gitlab.test/api/v4/events", json=[event("2026-08-10", 1, 1, "x")])
    _stub_user()
    _stub_project(1)
    responses.add(
        responses.GET,
        "http://gitlab.test/api/v4/projects/1/repository/commits",
        json=[merge_commit("m1", "2026-08-10", "dev-20260810", ["p1", "p2"])],
    )
    _stub_compare(1, "p1", "p2", [])

    commits = GitlabClient(GITLAB_CFG).list_commits(date(2026, 8, 1), date(2026, 8, 31))
    assert [c.title for c in commits] == ["合并分支 dev-20260810"]


@responses.activate
def test_the_same_commit_brought_in_twice_appears_once():
    responses.add(responses.GET, "http://gitlab.test/api/v4/events", json=[event("2026-08-10", 1, 1, "x")])
    _stub_user()
    _stub_project(1)
    responses.add(
        responses.GET,
        "http://gitlab.test/api/v4/projects/1/repository/commits",
        json=[
            merge_commit("m1", "2026-08-10", "dev-a", ["p1", "p2"]),
            merge_commit("m2", "2026-08-10", "dev-b", ["p3", "p4"]),
        ],
    )
    _stub_compare(1, "p1", "p2", [commit("shared", "2026-08-09", "feat: 同一条")])
    _stub_compare(1, "p3", "p4", [commit("shared", "2026-08-09", "feat: 同一条")])

    assert [c.sha for c in GitlabClient(GITLAB_CFG).list_commits(date(2026, 8, 1), date(2026, 8, 31))] == ["shared"]


@responses.activate
def test_ordinary_commits_are_untouched_by_expansion():
    responses.add(responses.GET, "http://gitlab.test/api/v4/events", json=[event("2026-08-10", 1, 1, "x")])
    _stub_user()
    _stub_project(1)
    responses.add(
        responses.GET,
        "http://gitlab.test/api/v4/projects/1/repository/commits",
        json=[commit("plain", "2026-08-10", "feat: 直接提交")],
    )

    commits = GitlabClient(GITLAB_CFG).list_commits(date(2026, 8, 1), date(2026, 8, 31))
    assert [c.sha for c in commits] == ["plain"]
    assert not any("compare" in c.request.url for c in responses.calls)
