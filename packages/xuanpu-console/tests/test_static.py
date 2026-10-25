"""控制台包：静态文件齐全，页面引用的资源都存在。"""

from __future__ import annotations

import hashlib
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
    referenced = {v.split('?')[0] if not v.startswith('?') else v for v in re.findall(r'(?:src|href)="([^"]+)"', html)}
    assert referenced == {
        "app.js", "identity.js", "reminders.js", "notification-alerts.js", "notification-center.js", "style.css",
        "?admin=1", "?", "workspace.css", "workspace.js", "workspace-forms.js",
        "collaboration-core.js", "handoffs.js", "handoff-results.js", "organizational-approvals.js", "collaboration-notifications.js",
        "shared-matters.js", "shared-materials.js", "shared-conversation.js", "shared-notifications.js",
        "connections.js", "organization.js", "organization-forms.js", "workspace-files.js", "workspace-chat.js", "proactive.js", "work-tasks.js", "task-definitions.js",
        "capabilities.js",
    }


def test_asset_urls_change_with_actual_content() -> None:
    """升级页面不能沿用浏览器缓存的旧脚本，地址指纹跟随实际资源字节。"""
    directory = static_dir()
    html = (directory / 'index.html').read_text()
    urls = re.findall(r'(?:src|href)="([^"?]+\.(?:js|css)(?:\?[^\"]*)?)"', html)
    assert urls
    for url in urls:
        name = url.split('?')[0]
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()[:16]
        assert url == name + '?v=' + digest, name


def test_console_only_talks_to_documented_endpoints() -> None:
    """页面调用的接口路径都在 HTTP 接口文档描述的范围内。"""
    script = "\n".join(path.read_text(encoding="utf-8") for path in static_dir().glob("*.js"))
    paths = set(re.findall(r'["`](/(?:v1/[a-z]+|healthz)[^"`?]*)', script))
    allowed_prefixes = (
        "/v1/capabilities", "/v1/installations", "/v1/secrets",
        "/v1/handoffs", "/v1/approval-policies", "/v1/admin/approval-policies", "/v1/approval-requests", "/v1/collaboration-notifications",
        "/v1/admin/organization", "/v1/organization", "/v1/admin/connections", "/healthz", "/v1/sessions", "/v1/packages", "/v1/audit", "/v1/usage",
        "/v1/shared-matters", "/v1/shared-contexts", "/v1/shared-notifications", "/v1/workspace", "/v1/me", "/v1/auth", "/v1/users", "/v1/assistant", "/v1/reminders", "/v1/notifications", "/v1/proactive", "/v1/work-tasks", "/v1/task-definitions",
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


def test_proactive_chat_routes_and_cleanup_are_connected() -> None:
    """业务跟进按需出现在对话，历史恢复与身份清理均有集成。"""
    directory = static_dir()
    html = (directory / "index.html").read_text(encoding="utf-8")
    app = (directory / "app.js").read_text(encoding="utf-8")
    history = (directory / "workspace-chat.js").read_text(encoding="utf-8")
    workspace = (directory / "workspace.js").read_text(encoding="utf-8")
    settings = (directory / "workspace-forms.js").read_text(encoding="utf-8")
    assert 'id="proactive-draft" class="reminder-preview hidden"' in html
    assert 'id="open-proactive-module"' in html
    assert "await handleProactiveRoute(route,text)" in app
    assert "dismissProactiveContext()" in app
    assert "metadata?.route?.proactive_draft" in history
    assert "restoreProactiveDraft(proactive.metadata.route.proactive_draft)" in history
    assert "resetProactive()" in workspace
    assert "renderProactiveSettings(form,settings)" in settings
    assert "...readProactive()" in settings
