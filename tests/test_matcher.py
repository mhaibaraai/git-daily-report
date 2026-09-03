"""项目名归一化与人工覆盖表。"""

from __future__ import annotations

import json

from git_daily_report.linking.matcher import load_mapping, normalize


def test_normalize_strips_brackets_and_phase_suffix():
    assert normalize("【应急】指挥平台（二期）") == "应急指挥平台"
    assert normalize("web-portal") == "webportal"


def test_normalize_folds_fullwidth_and_case():
    assert normalize("ＡＢＣ　Ｄ") == "abcd"


def test_normalize_handles_none():
    assert normalize(None) == ""
    assert normalize("") == ""


def test_mapping_override_bridges_naming_gap(tmp_path):
    (tmp_path / "project-mapping.json").write_text(
        json.dumps({"group/team/web-portal": "门户平台"}), encoding="utf-8"
    )
    mapping = load_mapping(tmp_path)

    assert mapping[normalize("group/team/web-portal")] == normalize("门户平台")


def test_load_mapping_missing_file_returns_empty(tmp_path):
    assert load_mapping(tmp_path) == {}


def test_load_mapping_tolerates_broken_json(tmp_path):
    (tmp_path / "project-mapping.json").write_text("{ not json", encoding="utf-8")
    assert load_mapping(tmp_path) == {}
