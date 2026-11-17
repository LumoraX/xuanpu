"""三成员会诊验收脚本的判定口径：瀑布交叠计数与用量完整性（脚本本身需真实模型，这里只测纯函数）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[5] / "scripts/acceptance"


def _script() -> Any:
    """按路径加载验收脚本。"""
    spec = importlib.util.spec_from_file_location("r5_consult_acceptance",
                                                  ROOT / "r5_consult_acceptance.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(offset: int, duration: int | None, kind: str = "dispatch", usage: bool = True) -> dict[str, Any]:
    """一行瀑布。"""
    return {"kind": kind, "offset_ms": offset, "duration_ms": duration, "usage_complete": usage}


def test_overlap_counts_pairs_and_running_rows() -> None:
    """三位成员两两交叠为 3 对；首尾相接不算交叠；运行中的行视为一直持续；只看派工行。"""
    script = _script()
    assert script.overlaps([_row(0, 100), _row(10, 100), _row(20, 100)]) == 3
    assert script.overlaps([_row(0, 100), _row(100, 50)]) == 0
    assert script.overlaps([_row(0, None), _row(500, 10)]) == 1
    assert script.overlaps([_row(0, 100), _row(10, 100, kind="tool")]) == 0


def test_usage_complete_ignores_running_rows() -> None:
    """已结束的执行行都要有完整用量；运行中的行不计。"""
    script = _script()
    assert script.usage_complete([_row(0, 10, kind="artisan"), _row(0, None, kind="artisan", usage=False)])
    assert not script.usage_complete([_row(0, 10, kind="artisan", usage=False)])


def test_usage_complete_only_counts_execution_rows() -> None:
    """工具、派工、编组行本来没有用量，不参与判定；没有执行行时不算完整。"""
    script = _script()
    rows = [_row(0, 10, kind="butler"), _row(1, 5, kind="artisan"),
            _row(1, 5, kind="tool", usage=False), _row(1, 9, kind="dispatch", usage=False),
            _row(1, 9, kind="group", usage=False)]
    assert script.usage_complete(rows)
    assert not script.usage_complete([_row(1, 9, kind="dispatch", usage=False)])
