"""真实模型回归集本身：条数、类别齐全、判定口径。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3] / "scripts/acceptance"


def _script() -> Any:
    """按路径加载回归脚本。"""
    spec = importlib.util.spec_from_file_location("r5_butler_regression", ROOT / "r5_butler_regression.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cases_cover_required_kinds() -> None:
    """至少 20 条，自办、该派、不该派、模糊四类都有。"""
    cases = json.loads((ROOT / "data/r5_butler_regression.json").read_text())["cases"]
    assert len(cases) >= 20
    assert {c["expect"] for c in cases} == {"self", "dispatch", "none", "ask"}
    assert len({c["id"] for c in cases}) == len(cases)


def test_judge_counts_misdispatch_and_missed() -> None:
    """该派没派是漏派；不该派却派了、派错包都是误派。"""
    judge = _script().judge
    sent = {"dispatches": [{"package": "support-desk"}]}
    assert judge({"expect": "dispatch", "package": "support-desk"}, sent) == "ok"
    assert judge({"expect": "dispatch", "package": "support-desk"}, {"dispatches": []}) == "missed"
    assert judge({"expect": "dispatch", "package": "finance"}, sent) == "misdispatch"
    assert judge({"expect": "self"}, sent) == "misdispatch"
    assert judge({"expect": "ask"}, {}) == "ok"
