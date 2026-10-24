"""协作页面真实脚本的权限、确认与通知行为测试。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from xuanpu_console import static_dir


def test_collaboration_native_behavior():
    """执行实际页面脚本，拒绝用页面可打开替代确认与身份隔离。"""
    node=shutil.which('node')
    if node is None:
        pytest.skip('缺少Node运行时')
    result=subprocess.run([node,str(Path(__file__).with_name('collaboration_behavior.cjs')),str(static_dir())],
        capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
