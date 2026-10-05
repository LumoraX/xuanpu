"""运行真实提醒脚本，回归提交竞争和通知初始化失败后的自动恢复。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from xuanpu_console import static_dir


def test_reminder_editor_and_notification_recovery() -> None:
    """旧提交不能清空新草稿；初次读取失败可恢复，退出清理连接与定时器。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("此脚本行为测试需要 Node.js；静态测试不需要")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("reminder_behavior.cjs")), str(static_dir())],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reminder behavior passed" in result.stdout, result.stdout + result.stderr
