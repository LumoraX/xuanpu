"""实际脚本验证人工确认、版本冲突和不确定重试边界。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from xuanpu_console import static_dir


def test_finance_behavior() -> None:
    """审批预检需独立本人确认；超时重试保持原计划和幂等键。"""
    node = shutil.which('node')
    assert node is not None
    result = subprocess.run([node, str(Path(__file__).with_name('finance_behavior.cjs')), str(static_dir())],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_finance_settlement_behavior() -> None:
    """付款与记账使用专用人工计划；目录、受理和未知结果有明确边界。"""
    node = shutil.which('node')
    assert node is not None
    result = subprocess.run(
        [node, str(Path(__file__).with_name('finance_settlement_behavior.cjs')), str(static_dir())],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_finance_compliance_behavior() -> None:
    """税务目标独立版本、专用审批和未知结果不能被页面重放。"""
    node = shutil.which('node')
    assert node is not None
    result = subprocess.run(
        [node, str(Path(__file__).with_name('finance_compliance_behavior.cjs')), str(static_dir())],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_finance_operations_behavior() -> None:
    """原生通知只由本人标记已读，打开原计划不执行且畸形计数不得归零。"""
    node = shutil.which('node')
    assert node is not None
    result = subprocess.run([node, str(Path(__file__).with_name('finance_operations_behavior.cjs')), str(static_dir())], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
