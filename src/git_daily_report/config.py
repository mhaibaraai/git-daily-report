"""配置加载与校验。

所有凭证只从 .env 读取，缺失即在页面上列出。凭证只留在服务端，不写入报告、
不打印到日志、不下发浏览器。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"
OUTPUT_DIR = PROJECT_ROOT / "output"

DEFAULT_AI_MODEL = "qwen3.8-flash"


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


@dataclass(frozen=True)
class GitlabConfig:
    base_url: str
    token: str


@dataclass(frozen=True)
class EomConfig:
    """EOM 只能在企微内登录，token 需在企微调试模式下用网页检查器取出来。"""

    base_url: str
    token: str


@dataclass(frozen=True)
class AiConfig:
    """走 OpenAI 兼容接口，换供应商只改 .env 不改代码。"""

    base_url: str
    api_key: str
    model: str


@dataclass(frozen=True)
class Config:
    gitlab: GitlabConfig
    eom: EomConfig
    ai: AiConfig

    def missing(self, need_ai: bool = True) -> list[str]:
        """列出缺哪些凭证，顺带说明去哪里拿；由 Claude 生成时不需要 AI 接口。"""
        gaps: list[str] = []
        if not self.eom.base_url:
            gaps.append("EOM_BASE_URL（EOM 后端接口地址，见 README）")
        if not self.eom.token:
            gaps.append("EOM_TOKEN（企微内打开 EOM，开启 webView 元素审查后复制请求头 token，见 README）")
        if not self.gitlab.base_url or not self.gitlab.token:
            gaps.append("GITLAB_BASE_URL / GITLAB_TOKEN（GitLab personal access token，勾 read_api）")
        if need_ai and not self.ai.api_key:
            gaps.append("AI_API_KEY（任意 OpenAI 兼容接口，配 AI_BASE_URL 与 AI_MODEL）")
        return gaps

    def require(self, need_ai: bool = True) -> None:
        gaps = self.missing(need_ai)
        if gaps:
            raise ConfigError(
                "缺少必要配置：\n  - " + "\n  - ".join(gaps) + f"\n请在 {ENV_PATH} 中补齐。"
            )


def load_config(env_path: Path | None = None) -> Config:
    """读取 .env 并组装配置对象，不做校验。"""
    load_dotenv(env_path or ENV_PATH, override=False)
    return Config(
        gitlab=GitlabConfig(
            base_url=os.getenv("GITLAB_BASE_URL", "").strip().rstrip("/"),
            token=os.getenv("GITLAB_TOKEN", "").strip(),
        ),
        eom=EomConfig(
            base_url=os.getenv("EOM_BASE_URL", "").strip().rstrip("/"),
            token=os.getenv("EOM_TOKEN", "").strip(),
        ),
        ai=AiConfig(
            base_url=os.getenv("AI_BASE_URL", "").strip(),
            api_key=os.getenv("AI_API_KEY", "").strip(),
            model=os.getenv("AI_MODEL", DEFAULT_AI_MODEL).strip() or DEFAULT_AI_MODEL,
        ),
    )
