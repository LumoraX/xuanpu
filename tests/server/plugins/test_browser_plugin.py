"""受控无头浏览器 L2 isolated_worker 插件安全与功能测试（ADR 0027）。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from server.api.app import create_app
from server.api.auth import caller
from server.core.errors import InvalidRequestError, PermissionDeniedError
from server.core.plugins.browser_plugin import (
    BrowserPolicy,
    BrowserSandboxBackendPlugin,
    BrowserSnapshot,
    validate_browser_url,
)
from server.core.tenancy import Principal
from server.domains.identity.models import UserRecord

_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0bIDATx\x9cc`\x00\x00\x00\x02\x00\x01"
    b"\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _FakeBrowserDriver:
    """确定性测试驱动：返回预置页面快照（含正常页、CAPTCHA 页与支付重定向页）。"""

    def __init__(self) -> None:
        self.closed = False
        self.interactions: list[list[dict[str, Any]]] = []

    async def read_page(self, url: str, *, timeout_seconds: float) -> BrowserSnapshot:
        _ = timeout_seconds
        if "captcha" in url:
            return BrowserSnapshot(final_url=url, title="Cloudflare Turnstile", text="Please verify you are human")
        return BrowserSnapshot(
            final_url=url,
            title="公开财报摘要",
            text="2026 Q4 营收增长 24%",
            screenshot_png=_PNG_1X1,
        )

    async def interact_page(
        self, url: str, actions: list[dict[str, Any]], *, timeout_seconds: float,
    ) -> BrowserSnapshot:
        _ = timeout_seconds
        self.interactions.append(actions)
        return BrowserSnapshot(final_url=url, title="已提交查询", text="查询结果：3 条记录", screenshot_png=_PNG_1X1)

    async def close(self) -> None:
        self.closed = True


def test_browser_url_guard_blocks_ssrf_non_allowlisted_and_payment() -> None:
    """硬阻断 SSRF 内网/云元数据 IP、非 HTTPS、未放行域名与支付页面。"""
    policy = BrowserPolicy(allowed_domains=("docs.example.com",))
    assert validate_browser_url("https://docs.example.com/report", policy) == "https://docs.example.com/report"

    for bad_url in (
        "http://docs.example.com/report",
        "file:///etc/passwd",
        "https://127.0.0.1/admin",
        "https://169.254.169.254/latest/meta-data/",
        "https://192.168.1.10/internal",
        "https://localhost/secret",
        "https://evil.other.com/page",
        "https://checkout.stripe.com/pay/cs_123",
        "https://docs.example.com/checkout/confirm",
    ):
        with pytest.raises(PermissionDeniedError):
            validate_browser_url(bad_url, policy)


async def test_browser_sandbox_plugin_enforces_isolation_captcha_stop_approval_and_taint_artifact(
    world: Any,
) -> None:
    """验证 isolated_worker 隔离、CAPTCHA 停机、写操作审批门禁与污点截图产物入库。"""
    platform = world.platform
    user_id = "usr_browser_alice"
    await platform.identity.store.create_user(
        UserRecord(
            user_id=user_id, tenant_id="acme", role="tenant_member", status="active",
            display_name="Alice", created_at=1000.0,
        )
    )
    who = Principal(role="tenant_member", tenant_id="acme", user_id=user_id, key_id=None)

    # 1. 热替换为带测试驱动与白名单的受控浏览器插件
    driver = _FakeBrowserDriver()
    plugin = BrowserSandboxBackendPlugin(
        policy=BrowserPolicy(allowed_domains=("docs.example.com",)),
        driver=driver,
        artifacts=platform.trace_artifacts,
    )
    await platform.plugins.hot_swap(plugin)

    # 验证 isolated_worker 上下文严禁直接触碰宿主 Platform 实例
    entry = platform.plugins._entries["browser-sandbox"]
    assert entry.ctx is not None
    with pytest.raises(InvalidRequestError, match="isolated_worker"):
        _ = entry.ctx.platform

    app = create_app(platform)
    app.dependency_overrides[caller] = lambda: who
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        # 2. 合法白名单只读抓取 -> 返回正文并发布带外部污点标记的 PNG 截图产物
        ok_resp = await http.post(
            "/v1/plugins/ext/browser-sandbox/read",
            json={"url": "https://docs.example.com/q4", "trace_id": "trc_br_1"},
        )
        assert ok_resp.status_code == 200, ok_resp.text
        body = ok_resp.json()
        assert body["status"] == "ok" and body["contains_external_input"] is True
        assert body["screenshot_artifact_id"] is not None
        arts = await platform.trace_artifacts.list_for_trace(who, "trc_br_1")
        assert len(arts) == 1 and arts[0].contains_external_input is True and arts[0].mime_type == "image/png"

        # 3. 命中 CAPTCHA -> 立即停机返回 blocked_captcha
        cap_resp = await http.post(
            "/v1/plugins/ext/browser-sandbox/read",
            json={"url": "https://docs.example.com/captcha-gate"},
        )
        assert cap_resp.status_code == 200
        assert cap_resp.json()["status"] == "blocked_captcha"

        # 4. 写交互未经人工批准 -> 返回 approval_required，绝不执行驱动交互
        need_app = await http.post(
            "/v1/plugins/ext/browser-sandbox/interact",
            json={"url": "https://docs.example.com/form", "actions": [{"op": "click", "selector": "#submit"}]},
        )
        assert need_app.status_code == 200 and need_app.json()["status"] == "approval_required"
        assert driver.interactions == []

        # 5. 经显式人工批准后放行交互
        done_app = await http.post(
            "/v1/plugins/ext/browser-sandbox/interact",
            json={
                "url": "https://docs.example.com/form",
                "actions": [{"op": "click", "selector": "#submit"}],
                "approved": True,
                "approval_id": "apr_user_1",
            },
        )
        assert done_app.status_code == 200 and done_app.json()["status"] == "ok"
        assert len(driver.interactions) == 1

    # 6. Clean Unmounting：卸载时关闭底层浏览器驱动
    await platform.plugins.unmount("browser-sandbox")
    assert driver.closed is True
