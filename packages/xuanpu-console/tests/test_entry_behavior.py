"""极简入口：真实脚本行为（Node）与静态约束（资源齐全、指纹、只调文档里的接口、不加载功能页脚本）。"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from xuanpu_console import static_dir

ENTRY_SCRIPTS = ["entry.js", "cards.js", "module-cards.js", "collab-cards.js", "progress-card.js", "dashboard.js", "tips.js", "entry-boot.js"]


def test_entry_behavior() -> None:
    """概览、发送与确认卡、列表卡带版本、进展卡与刷新恢复、身份切换丢弃旧结果、管家不可用保留输入。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("此脚本行为测试需要 Node.js；静态测试不需要")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("entry_behavior.cjs")), str(static_dir())],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "entry behavior passed" in result.stdout, result.stdout + result.stderr


def test_entry_assets_and_fingerprints() -> None:
    """新页只引用自己的脚本与样式，地址指纹跟随实际字节；默认不加载功能页脚本。"""
    directory = static_dir()
    html = (directory / "entry.html").read_text(encoding="utf-8")
    urls = re.findall(r'(?:src|href)="([^"]+\.(?:js|css)\?v=[0-9a-f]{16})"', html)
    names = [url.split("?")[0] for url in urls]
    assert names == ["entry.css", *ENTRY_SCRIPTS]
    for url in urls:
        name = url.split("?")[0]
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()[:16]
        assert url == name + "?v=" + digest, name
    for classic in ("app.js", "workspace.js", "reminders.js", "finance.js"):
        assert classic not in html


def test_entry_has_four_parts_and_more_link() -> None:
    """首屏四样（概览、对话、提示、输入）；「更多」进入功能页兜底。"""
    html = (static_dir() / "entry.html").read_text(encoding="utf-8")
    for part in ('id="overview"', 'id="transcript"', 'id="tips"', 'id="composer"'):
        assert part in html
    assert 'href="index.html?classic=1"' in html and ">更多<" in html


def test_entry_only_uses_original_endpoints() -> None:
    """卡片调用的都是原模块接口；不新增写接口。"""
    script = "\n".join((static_dir() / name).read_text(encoding="utf-8") for name in ENTRY_SCRIPTS)
    paths = set(re.findall(r'["`](/v1/[a-z-]+(?:/[a-z-]+)?)', script))
    assert paths <= {
        "/v1/me", "/v1/auth/logout", "/v1/auth/feishu", "/v1/workspace/messages", "/v1/workspace/items",
        "/v1/workspace/files", "/v1/assistant/messages", "/v1/assistant/proposals", "/v1/assistant/pending",
        "/v1/reminders", "/v1/notifications", "/v1/work-tasks", "/v1/traces", "/v1/dashboard",
        "/v1/proactive", "/v1/task-definitions", "/v1/me/connections", "/v1/shared-matters",
        "/v1/handoffs", "/v1/approval-requests", "/v1/organization/people", "/v1/approval-policies",
    }, paths
