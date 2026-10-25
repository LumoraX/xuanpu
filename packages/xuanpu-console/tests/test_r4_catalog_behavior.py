"""能力原生管理脚本必须通过实际Node行为反例。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from xuanpu_console import static_dir


def test_r4_catalog_native_behavior():
    """运行实际脚本，不以DOM文本存在代替确认与身份隔离。"""
    node = shutil.which('node')
    if node is None:
        pytest.skip('没有Node，原生行为未执行')
    result = subprocess.run([node, str(Path(__file__).with_name('r4_catalog_behavior.cjs')), str(static_dir())],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
