"""共享辅助入口使用实际脚本验证原生确认和权限切换。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from xuanpu_console import static_dir


def test_shared_native_behavior():
    """固定版本选择、重试及身份隔离不能用页面HTTP成功替代。"""
    node = shutil.which('node')
    if node is None:
        pytest.skip('缺少Node运行时')
    result = subprocess.run([node, str(Path(__file__).with_name('shared_matter_behavior.cjs')), str(static_dir())],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
