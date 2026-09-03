"""Gitee 企业工作项读取。

走 gitee.com/api/v5 的企业接口，普通个人访问令牌即可（需 enterprises 权限），
不需要 api.gitee.com 那套企业 OpenAPI 授权。

时间过滤的坑：created_at / finished_at / deadline 的范围写法实测一律返回 422
「时间格式不正确」，唯一可用的是单边下界 since，上界只能在本地过滤。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..config import GiteeConfig
from ..models import WorkItem
from ._http import SourceError, request

API_BASE = "https://gitee.com/api/v5"
PER_PAGE = 100
MAX_PAGES = 50


class GiteeError(SourceError):
    """Gitee 接口返回错误。"""


def _iso(day: date, end_of_day: bool = False) -> str:
    suffix = "T23:59:59+08:00" if end_of_day else "T00:00:00+08:00"
    return f"{day.isoformat()}{suffix}"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class GiteeClient:
    def __init__(self, cfg: GiteeConfig) -> None:
        self._cfg = cfg

    def _get(self, path: str, params: dict[str, Any]) -> tuple[Any, int]:
        response = request(
            "GET",
            f"{API_BASE}{path}",
            headers={"Authorization": f"token {self._cfg.access_token}"},
            params=params,
            source="Gitee",
        )
        if response.status_code == 401:
            raise GiteeError("Gitee token 无效或已过期，请检查 GITEE_ACCESS_TOKEN。")
        if response.status_code == 403:
            raise GiteeError("Gitee token 权限不足，需要 enterprises 权限。")
        if response.status_code >= 400:
            raise GiteeError(f"Gitee 请求失败：HTTP {response.status_code} {response.text[:200]}")
        total_page = int(response.headers.get("total_page") or 1)
        return response.json(), total_page

    def list_enterprise_issues(self, since: date, until: date) -> list[WorkItem]:
        """拉本人名下的企业工作项，上界在本地过滤。"""
        collected: list[WorkItem] = []
        upper = datetime.fromisoformat(_iso(until, end_of_day=True))
        page = 1
        while page <= MAX_PAGES:
            payload, total_page = self._get(
                f"/enterprises/{self._cfg.enterprise}/issues",
                {
                    "state": "all",
                    "assignee": self._cfg.assignee,
                    "since": _iso(since),
                    "page": page,
                    "per_page": PER_PAGE,
                },
            )
            if not isinstance(payload, list):
                break
            for raw in payload:
                if isinstance(raw, dict):
                    collected.append(WorkItem.from_api(raw))
            if page >= total_page or not payload:
                break
            page += 1

        return [item for item in collected if _within_upper(item, upper)]


def _within_upper(item: WorkItem, upper: datetime) -> bool:
    moment = _parse_dt(item.updated_at) or _parse_dt(item.created_at)
    return moment is None or moment <= upper

