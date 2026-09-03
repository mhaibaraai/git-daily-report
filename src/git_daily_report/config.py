"""配置加载与校验。

所有凭证只从 .env 读取，按本次启用的数据源分别校验，缺失即中止。凭证只留在
服务端，不写入报告、不打印到日志、不下发浏览器。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"
OUTPUT_DIR = PROJECT_ROOT / "output"
CONFIG_DIR = PROJECT_ROOT / "config"

ALL_SOURCES = ("gitee", "gitlab")

DEFAULT_AI_MODEL = "qwen-plus"


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


@dataclass(frozen=True)
class GiteeConfig:
    access_token: str
    enterprise: str
    assignee: str


@dataclass(frozen=True)
class GitlabConfig:
    base_url: str
    token: str


@dataclass(frozen=True)
class AiConfig:
    """走 OpenAI 兼容接口，换供应商只改 .env 不改代码。"""

    base_url: str
    api_key: str
    model: str

    @property
    def ready(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class Config:
    gitee: GiteeConfig
    gitlab: GitlabConfig
    ai: AiConfig

    def missing(self, sources: tuple[str, ...]) -> list[str]:
        """列出指定数据源缺哪些凭证，顺带说明去哪里拿。"""
        gaps: list[str] = []
        if "gitee" in sources:
            if not self.gitee.access_token:
                gaps.append(
                    "GITEE_ACCESS_TOKEN（gitee.com/profile/personal_access_tokens，勾 enterprises）"
                )
            if not self.gitee.enterprise:
                gaps.append("GITEE_ENTERPRISE（企业主页 gitee.com/enterprises/<这一段>）")
            if not self.gitee.assignee:
                gaps.append("GITEE_ASSIGNEE（你的 Gitee 登录名）")
        if "gitlab" in sources and not self.gitlab.token:
            gaps.append("GITLAB_TOKEN（GitLab 实例的 personal access token，勾 read_api）")
        return gaps

    def require(self, sources: tuple[str, ...]) -> None:
        gaps = self.missing(sources)
        if gaps:
            raise ConfigError(
                "缺少必要配置：\n  - " + "\n  - ".join(gaps) + f"\n请在 {ENV_PATH} 中补齐。"
            )


def load_config(env_path: Path | None = None) -> Config:
    """读取 .env 并组装配置对象，不做校验。"""
    load_dotenv(env_path or ENV_PATH, override=False)
    return Config(
        gitee=GiteeConfig(
            access_token=os.getenv("GITEE_ACCESS_TOKEN", "").strip(),
            enterprise=os.getenv("GITEE_ENTERPRISE", "").strip(),
            assignee=os.getenv("GITEE_ASSIGNEE", "").strip(),
        ),
        gitlab=GitlabConfig(
            base_url=os.getenv("GITLAB_BASE_URL", "").strip().rstrip("/"),
            token=os.getenv("GITLAB_TOKEN", "").strip(),
        ),
        ai=AiConfig(
            base_url=os.getenv("AI_BASE_URL", "").strip(),
            api_key=os.getenv("AI_API_KEY", "").strip(),
            model=os.getenv("AI_MODEL", DEFAULT_AI_MODEL).strip() or DEFAULT_AI_MODEL,
        ),
    )


def parse_sources(raw: str | None) -> tuple[str, ...]:
    """解析 --sources 参数，未指定时启用全部。"""
    if not raw:
        return ALL_SOURCES
    names = tuple(n.strip() for n in raw.split(",") if n.strip())
    unknown = [n for n in names if n not in ALL_SOURCES]
    if unknown:
        raise ConfigError(f"未知数据源：{', '.join(unknown)}；可选：{', '.join(ALL_SOURCES)}")
    return names
