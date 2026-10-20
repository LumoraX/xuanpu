"""主动业务前端交互的真实脚本回归。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from xuanpu_console import static_dir


def test_proactive_confirmation_notifications_and_identity() -> None:
    """时间确认、幂等重试、历史核验、身份隔离及免打扰补发。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("脚本行为测试需要 Node.js")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("proactive_behavior.cjs")), str(static_dir())],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "proactive behavior passed" in result.stdout
