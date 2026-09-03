"""共用的 HTTP 请求封装：超时、重试与退避。"""

from __future__ import annotations

import time
from typing import Any, Mapping

import requests

DEFAULT_TIMEOUT = 30
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 1.5
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class SourceError(RuntimeError):
    """数据源请求失败。"""


def request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    json_body: Any | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    source: str = "",
) -> requests.Response:
    """发请求并对限流、5xx 与网络抖动做有限重试。"""
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.request(
                method,
                url,
                headers=dict(headers or {}),
                params=dict(params or {}),
                json=json_body,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_error = exc
            if attempt == MAX_ATTEMPTS:
                break
            time.sleep(BACKOFF_SECONDS * attempt)
            continue

        if response.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF_SECONDS * attempt)
            continue
        return response

    raise SourceError(f"{source or url} 请求失败：{last_error}") from last_error
