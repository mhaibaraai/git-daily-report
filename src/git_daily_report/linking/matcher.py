"""项目名归一化与人工覆盖表。

GitLab 的仓库路径与 Gitee 的项目名写法常有出入（全角括号、期数后缀、连字符），
归一化把这些差异抹平；抹不平的用 config/project-mapping.json 显式覆盖，这是
归属判定唯一的确定性手段。
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Mapping

MAPPING_FILENAME = "project-mapping.json"

_NOISE = re.compile(r"[\s【】\[\]（）()·、,，_\-—/\\]")
_PHASE_SUFFIX = re.compile(r"(?:第?[一二三四五六七八九十\d]+期)$")


def normalize(name: str | None) -> str:
    """归一化项目名：全角转半角、去括号与标点、去「（1期）」类后缀。"""
    if not name:
        return ""
    folded = unicodedata.normalize("NFKC", name)
    cleaned = _NOISE.sub("", folded).lower()
    return _PHASE_SUFFIX.sub("", cleaned)


def load_mapping(config_dir: Path) -> dict[str, str]:
    """读取人工覆盖表：GitLab 项目名 → Gitee 项目名。"""
    path = config_dir / MAPPING_FILENAME
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    return {normalize(str(k)): normalize(str(v)) for k, v in raw.items() if k and v}
