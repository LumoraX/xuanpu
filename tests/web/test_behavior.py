"""运行实际浏览器脚本的凭据与异步隔离测试（无需前端生产依赖）。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from web import static_dir


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
    assert "identity behavior passed" in result.stdout, result.stdout + result.stderr


def test_password_account_flows() -> None:
    """设置链接令牌从地址栏抹掉；设口令、口令登录只经同源 cookie 请求，不带 API key。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("此脚本行为测试需要 Node.js；静态测试不需要")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("accounts_behavior.cjs")), str(static_dir())],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "accounts behavior passed" in result.stdout, result.stdout + result.stderr


def test_secrets_panel_flows() -> None:
    """租户密钥管理面板：名称校验、写入后清空明文、二次确认删除与身份切换清理。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("此脚本行为测试需要 Node.js；静态测试不需要")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("secrets_behavior.cjs")), str(static_dir())],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "secrets behavior passed" in result.stdout, result.stdout + result.stderr


def test_marketplace_panel_flows() -> None:
    """能力市场（匠人市场 + Skill 市场）：分类切换、一键安装、缺失密钥跳转与身份重置。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("此脚本行为测试需要 Node.js；静态测试不需要")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("marketplace_behavior.cjs")), str(static_dir())],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "marketplace behavior passed" in result.stdout, result.stdout + result.stderr
