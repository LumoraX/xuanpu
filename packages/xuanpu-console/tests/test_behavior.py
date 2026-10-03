"""运行实际浏览器脚本的凭据与异步隔离测试（无需前端生产依赖）。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from xuanpu_console import static_dir


def test_identity_request_isolation() -> None:
    """API key 不携带 cookie，员工模式不携带 key，身份切换后拒绝旧响应。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("此脚本行为测试需要 Node.js；静态测试不需要")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("identity_behavior.cjs")), str(static_dir())],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
