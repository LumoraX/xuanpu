"""知识与记忆原生脚本必须通过实际 Node 行为反例。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from web import static_dir


def test_r4_knowledge_native_behavior():
    """运行实际脚本：迟到响应不回填、重试沿用操作号、依据失效先清屏。"""
    node = shutil.which('node')
    if node is None:
        pytest.skip('没有Node，原生行为未执行')
    result = subprocess.run([node, str(Path(__file__).with_name('r4_knowledge_behavior.cjs')), str(static_dir())],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
