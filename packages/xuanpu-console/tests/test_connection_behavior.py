"""无模型、浏览器身份切换及原生授权确认行为。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from xuanpu_console import static_dir


def test_connection_behavior():
    """真实脚本在DOM夹具中不能自动同意或显示迟到资料。"""
    node=shutil.which('node')
    if node is None:
        import pytest
        pytest.skip('没有Node运行时')
    result=subprocess.run([node,str(Path(__file__).with_name('connection_behavior.cjs')),str(static_dir())],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
