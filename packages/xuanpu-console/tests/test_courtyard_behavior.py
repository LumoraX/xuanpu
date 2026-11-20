"""小院（R7）：纯模型、角色画法与进展卡接入的真实脚本行为（Node）。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from xuanpu_console import static_dir


def test_courtyard_behavior() -> None:
    """家具归类、节拍顺序、经过条目、等你提示、快照终态、降级与角色形象。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("此脚本行为测试需要 Node.js；静态测试不需要")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("courtyard_behavior.cjs")), str(static_dir())],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "courtyard model passed" in result.stdout, result.stdout + result.stderr
