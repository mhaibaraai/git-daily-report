"""企业 GitLab 提交读取。

推送事件（/api/v4/events?action=pushed）一次调用就能拿到本人跨全部项目的活动，
用它圈定「这段时间我推过哪些项目」，再只对这些项目拉 /repository/commits 取逐条
提交，避免遍历全部仓库。实例通常部署在内网，脱离内网时该源应被跳过。

两个接口的时间语义不同：events 的 after/before 是开区间的日期，commits 的
since/until 是闭区间的 ISO8601 时间戳，不能互相照搬。

合并提交要展开：本人的提交里绝大多数是把开发分支合进 dev 的 Merge，标题本身
没有信息量，但它带进来的那批提交才是当天真正集成的内容。用 parent_ids 调
compare 取出来，归到合并当天。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from ..config import GitlabConfig
from ..models import Commit
from ._http import SourceError, request

PER_PAGE = 100
MAX_PAGES = 20

# 单次合并带进来的提交上限，防止合并长期分支时刷屏
MAX_MERGE_SOURCES = 50

_MERGE_BRANCH = re.compile(r"Merge branch '([^']+)'")


class GitlabError(SourceError):
    """GitLab 接口返回错误。"""


class GitlabUnreachable(GitlabError):
    """GitLab 实例不可达，通常是不在内网。"""


class GitlabClient:
    def __init__(self, cfg: GitlabConfig) -> None:
        self._cfg = cfg
        self._project_names: dict[int, str] = {}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, str | None]:
        try:
            response = request(
                "GET",
                f"{self._cfg.base_url}/api/v4{path}",
                headers={"PRIVATE-TOKEN": self._cfg.token},
                params=params or {},
                source="GitLab",
            )
        except SourceError as exc:
            raise GitlabUnreachable(
                f"GitLab 不可达（{self._cfg.base_url}），是否不在内网或 VPN？"
            ) from exc
        if response.status_code == 401:
            raise GitlabError("GitLab token 无效或已过期，请检查 GITLAB_TOKEN。")
        if response.status_code >= 400:
            raise GitlabError(f"GitLab 请求失败：HTTP {response.status_code}")
        return response.json(), response.headers.get("X-Next-Page") or None

    def _project_name(self, project_id: int) -> str:
        if project_id not in self._project_names:
            try:
                payload, _ = self._get(f"/projects/{project_id}")
                name = payload.get("path_with_namespace") or payload.get("name") or ""
            except GitlabError:
                name = ""
            self._project_names[project_id] = name or f"project-{project_id}"
        return self._project_names[project_id]

    def list_push_events(self, since: date, until: date) -> list[dict[str, Any]]:
        """after / before 在 GitLab 侧是开区间，这里各外扩一天。"""
        events: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_PAGES:
            payload, next_page = self._get(
                "/events",
                {
                    "action": "pushed",
                    "after": (since - timedelta(days=1)).isoformat(),
                    "before": (until + timedelta(days=1)).isoformat(),
                    "per_page": PER_PAGE,
                    "page": page,
                },
            )
            if not isinstance(payload, list) or not payload:
                break
            events.extend(e for e in payload if isinstance(e, dict))
            if not next_page:
                break
            page = int(next_page)
        return events

    def current_user(self) -> dict[str, Any]:
        """当前令牌对应的账号，用于把提交过滤成本人的。"""
        payload, _ = self._get("/user")
        return payload if isinstance(payload, dict) else {}

    def _identities(self) -> set[str]:
        """本人可用于比对提交作者的标识集合，取不到任何一项时为空。"""
        user = self.current_user()
        fields = (user.get("email"), user.get("commit_email"), user.get("name"), user.get("username"))
        return {str(v).strip().lower() for v in fields if v and str(v).strip()}

    def _project_commits(self, project_id: int, since: date, until: date) -> list[dict[str, Any]]:
        """拉单个项目在区间内的提交，all=true 覆盖全部分支。"""
        collected: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_PAGES:
            payload, next_page = self._get(
                f"/projects/{project_id}/repository/commits",
                {
                    "since": _iso(since),
                    "until": _iso(until, end_of_day=True),
                    "all": "true",
                    "per_page": PER_PAGE,
                    "page": page,
                },
            )
            if not isinstance(payload, list) or not payload:
                break
            collected.extend(c for c in payload if isinstance(c, dict))
            if not next_page:
                break
            page = int(next_page)
        return collected

    def _merge_sources(self, project_id: int, raw: dict[str, Any]) -> list[dict[str, Any]]:
        """合并带进来的提交：第一父到第二父之间那一段。"""
        parents = raw.get("parent_ids") or []
        if len(parents) < 2:
            return []
        try:
            payload, _ = self._get(
                f"/projects/{project_id}/repository/compare",
                {"from": parents[0], "to": parents[1]},
            )
        except GitlabError:
            return []
        commits = payload.get("commits") if isinstance(payload, dict) else None
        if not isinstance(commits, list):
            return []
        return [c for c in commits[:MAX_MERGE_SOURCES] if isinstance(c, dict)]

    def list_commits(self, since: date, until: date) -> list[Commit]:
        """先用推送事件圈定项目，再逐项目取本人的提交，合并提交就地展开。"""
        project_ids = sorted(
            {
                event["project_id"]
                for event in self.list_push_events(since, until)
                if isinstance(event.get("project_id"), int)
            }
        )
        if not project_ids:
            return []

        identities = self._identities()
        low, high = since.isoformat(), until.isoformat()
        commits: list[Commit] = []

        seen: set[tuple[str, str]] = set()

        for project_id in project_ids:
            name = self._project_name(project_id)
            for raw in self._project_commits(project_id, since, until):
                day = _commit_day(raw)
                if not day or not (low <= day <= high):
                    continue
                if not _is_authored_by(raw, identities):
                    continue
                for commit in self._as_commits(project_id, name, raw, day):
                    key = (name, commit.sha)
                    if key not in seen:
                        seen.add(key)
                        commits.append(commit)
        return commits

    def _as_commits(
        self, project_id: int, name: str, raw: dict[str, Any], day: str
    ) -> list[Commit]:
        """一条原始提交展开成进日报的若干条，日期一律归到本人操作的那天。"""
        brought_in = self._merge_sources(project_id, raw)
        if brought_in:
            # 带进来的提交常常是同事写的，集成也是当天的工作，不按作者再滤一遍
            return [_to_commit(name, source, day) for source in brought_in]
        return [_to_commit(name, raw, day, title=_fallback_title(raw))]


def _to_commit(project: str, raw: dict[str, Any], day: str, title: str | None = None) -> Commit:
    text = title if title is not None else (raw.get("title") or "").strip()
    return Commit(
        project=project,
        sha=str(raw.get("id") or ""),
        day=day,
        title=text,
        message=text if title is not None else (raw.get("message") or "").strip(),
        web_url=raw.get("web_url") or "",
    )


def _fallback_title(raw: dict[str, Any]) -> str | None:
    """展不开的合并提交改写成分支名，不让这条痕迹凭空消失。"""
    match = _MERGE_BRANCH.match((raw.get("title") or "").strip())
    return f"合并分支 {match.group(1)}" if match else None


def _iso(day: date, end_of_day: bool = False) -> str:
    suffix = "T23:59:59+08:00" if end_of_day else "T00:00:00+08:00"
    return f"{day.isoformat()}{suffix}"


def _commit_day(raw: dict[str, Any]) -> str | None:
    value = raw.get("committed_date") or raw.get("created_at")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _is_authored_by(raw: dict[str, Any], identities: set[str]) -> bool:
    """身份取不到时一律保留——宁可多给也不静默丢弃素材。"""
    if not identities:
        return True
    author = {
        str(raw.get(field) or "").strip().lower()
        for field in ("author_email", "author_name", "committer_email", "committer_name")
    }
    return bool(author & identities)



def _event_day(event: dict[str, Any]) -> str | None:
    created = event.get("created_at")
    if not isinstance(created, str):
        return None
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None

