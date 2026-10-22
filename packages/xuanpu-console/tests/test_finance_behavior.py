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


def test_finance_funding_behavior() -> None:
    """专用资金页面使用真实收件与受控账户，收尾不能再次付款。"""
    node = shutil.which('node')
    assert node is not None
    result = subprocess.run([node, str(Path(__file__).with_name('finance_funding_behavior.cjs')), str(static_dir())], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_finance_records_behavior() -> None:
    """内部报销保留原文、真实可选附件及版本历史，且不暴露外部执行。"""
    node = shutil.which('node')
    assert node is not None
    result = subprocess.run(
        [node, str(Path(__file__).with_name('finance_records_behavior.cjs')), str(static_dir())],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_finance_materials_behavior() -> None:
    """收件跨页恢复保留请求身份，附件点选不丢未保存单据。"""
    node = shutil.which('node')
    assert node is not None
    result = subprocess.run(
        [node, str(Path(__file__).with_name('finance_materials_behavior.cjs')), str(static_dir())],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_finance_records_list_behavior() -> None:
    """能力失败关闭，摘要筛选、分页与未决写入恢复保持身份边界。"""
    node = shutil.which('node')
    assert node is not None
    result = subprocess.run(
        [node, str(Path(__file__).with_name('finance_records_list_behavior.cjs')), str(static_dir())],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
