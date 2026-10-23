"""组织入口在无模型及身份切换时保持人工确认边界。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from xuanpu_console import static_dir


def test_organization_native_confirmation_behavior():
    """实际脚本验证切换身份丢弃迟到响应、重试同一请求以及冲突不自动覆盖。"""
    script = static_dir() / 'organization.js'
    assert script.is_file(), '缺少无模型组织入口'
    node = shutil.which('node')
    if node is None:
        import pytest
        pytest.skip('没有Node运行时')
    result = subprocess.run([node, str(Path(__file__).with_name('organization_behavior.cjs')), str(static_dir())],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
