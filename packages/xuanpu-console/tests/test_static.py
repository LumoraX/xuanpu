"""控制台包：静态文件齐全，页面引用的资源都存在。"""

from __future__ import annotations

import re

from xuanpu_console import static_dir


def test_static_files_present() -> None:
    """静态文件都在，页面引用的脚本与样式就是它们。"""
    directory = static_dir()
    assert (directory / "index.html").is_file()
    assert (directory / "app.js").is_file()
    assert (directory / "identity.js").is_file()
    assert (directory / "style.css").is_file()
    html = (directory / "index.html").read_text(encoding="utf-8")
    referenced = set(re.findall(r'(?:src|href)="([^"]+)"', html))
    assert referenced == {"app.js", "identity.js", "style.css", "?admin=1", "?"}


def test_console_only_talks_to_documented_endpoints() -> None:
    """页面调用的接口路径都在 HTTP 接口文档描述的范围内。"""
    script = "\n".join(path.read_text(encoding="utf-8") for path in static_dir().glob("*.js"))
    paths = set(re.findall(r'["`](/(?:v1/[a-z]+|healthz)[^"`?]*)', script))
    allowed_prefixes = (
        "/healthz", "/v1/sessions", "/v1/packages", "/v1/audit", "/v1/usage",
        "/v1/me", "/v1/auth", "/v1/users", "/v1/assistant",
    )  # fmt: skip
    assert paths
    assert all(path.startswith(allowed_prefixes) for path in paths), paths


def test_console_can_stop_a_streaming_turn() -> None:
    """对话框有「停止」按钮：拿到这一轮的 submission_id 才可用，只停这一轮。"""
    html = (static_dir() / "index.html").read_text(encoding="utf-8")
    script = (static_dir() / "app.js").read_text(encoding="utf-8")
    assert re.search(r'<button[^>]*id="stop-btn"[^>]*>停止</button>', html)
    assert "/v1/sessions/${target.sessionId}/cancel`" in script
    # 带上这一轮的 submission_id，不发空请求体（那会停掉同一会话里别处发起的对话）
    assert "json: { submission_id: target.submissionId }" in script
    assert "json: {} }" not in script
    # 收到 turn 帧才启用
    assert 'if (name === "turn") {' in script


def test_identity_controls_exist() -> None:
    """登录入口、员工身份、退出、用户过滤和分页都具备可访问的控件。"""
    html = (static_dir() / "index.html").read_text(encoding="utf-8")
    for name in ("feishu-login", "login-error", "current-user", "logout", "tab-users",
                 "users-status", "users-role", "users-unverified", "users-more"):
        assert f'id="{name}"' in html
    assert "用飞书登录" in html
