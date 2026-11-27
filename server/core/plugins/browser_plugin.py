"""受控无头浏览器 L2 隔离插件（``isolated_worker`` 信任级，ADR 0027）。

安全红线：
1. 强制域名白名单 + SSRF/内网 IP/云元数据地址（169.254.169.254 等）硬阻断；
2. 遇到人机验证/CAPTCHA 或支付结账页立即停机并回传给员工处理，严禁自动绕过；
3. 读操作（文本抽取 + 截图）自动打 ``contains_external_input=True`` 外部污点标记并可发布为过程产物；
4. 写交互（点击/填表/提交）未经显式人工批准一律返回 ``approval_required`` 暂停态。
"""

from __future__ import annotations

import ipaddress
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from server.core.errors import InvalidRequestError, PermissionDeniedError, RuntimeUnavailableError
from server.core.plugins.models import BackendPluginManifest, PluginHealth
from server.engine.tracing.models import TraceOwner

if TYPE_CHECKING:
    from server.core.plugins.host import PluginContext
    from server.core.tenancy import Principal
    from server.engine.artifacts.service import TraceArtifactService

_CAPTCHA_RE = re.compile(
    r"recaptcha|hcaptcha|cloudflare\s+turnstile|verify\s+you\s+are\s+human|人机验证|滑动拼图",
    re.IGNORECASE,
)
_PAYMENT_HOST_SUFFIXES = (
    "stripe.com",
    "checkout.stripe.com",
    "alipay.com",
    "pay.weixin.qq.com",
    "paypal.com",
)
_PAYMENT_PATH_RE = re.compile(r"/(?:checkout|payment|pay|billing/confirm)\b", re.IGNORECASE)


class BrowserPolicy(BaseModel):
    """受控浏览器插件运行策略（默认零出网白名单，须管理员显式放行域名）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    allowed_domains: tuple[str, ...] = ()
    allow_http: bool = False
    max_text_chars: int = Field(default=12_000, ge=500, le=50_000)
    timeout_seconds: float = Field(default=20.0, ge=2.0, le=60.0)


class BrowserSnapshot(BaseModel):
    """单次受控浏览返回快照（强制标记为不可信外部输入）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    final_url: str
    title: str = ""
    text: str = ""
    screenshot_png: bytes | None = None
    contains_external_input: bool = True


class BrowserDriver(Protocol):
    """无头浏览器底层执行器接口（生产走 Playwright 隔离容器，测试可注入确定性实现）。"""

    async def read_page(self, url: str, *, timeout_seconds: float) -> BrowserSnapshot:
        """以只读模式打开目标 URL，提取可见正文与可选 PNG 截图。"""
        ...

    async def interact_page(
        self, url: str, actions: list[dict[str, Any]], *, timeout_seconds: float,
    ) -> BrowserSnapshot:
        """在已批准前提下执行有限交互步骤并返回最终页面快照。"""
        ...

    async def close(self) -> None:
        """释放浏览器进程与上下文资源。"""
        ...


class PlaywrightBrowserDriver:
    """基于 Playwright Chromium 的真实无头浏览器驱动（懒加载，无依赖时报错提示）。"""

    async def read_page(self, url: str, *, timeout_seconds: float) -> BrowserSnapshot:
        """无头打开页面、抽取文本与 PNG 截图。"""
        try:
            from playwright.async_api import async_playwright  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeUnavailableError(
                "当前节点未安装 Playwright 浏览器执行环境，请在 browser-sandbox 容器中启用"
            ) from exc

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_seconds * 1000))
                title = await page.title()
                body_text = await page.inner_text("body")
                png = await page.screenshot(type="png", full_page=False)
                return BrowserSnapshot(
                    final_url=page.url,
                    title=title,
                    text=body_text,
                    screenshot_png=png,
                )
            finally:
                await browser.close()

    async def interact_page(
        self, url: str, actions: list[dict[str, Any]], *, timeout_seconds: float,
    ) -> BrowserSnapshot:
        """按白名单交互动作（click/fill）操作页面并返回最新快照。"""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeUnavailableError(
                "当前节点未安装 Playwright 浏览器执行环境，请在 browser-sandbox 容器中启用"
            ) from exc

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_seconds * 1000))
                for step in actions[:10]:
                    op = str(step.get("op") or "")
                    selector = str(step.get("selector") or "")
                    if op == "click" and selector:
                        await page.click(selector, timeout=5000)
                    elif op == "fill" and selector:
                        await page.fill(selector, str(step.get("value") or ""), timeout=5000)
                title = await page.title()
                body_text = await page.inner_text("body")
                png = await page.screenshot(type="png", full_page=False)
                return BrowserSnapshot(
                    final_url=page.url,
                    title=title,
                    text=body_text,
                    screenshot_png=png,
                )
            finally:
                await browser.close()

    async def close(self) -> None:
        """按次启停模式无需持久进程清理。"""
        return None


def validate_browser_url(url: str, policy: BrowserPolicy) -> str:
    """严格校验目标 URL：阻断非 HTTP(S)、SSRF 内网/云元数据 IP、未授权域名与支付域名。"""
    cleaned = url.strip()
    parts = urlsplit(cleaned)
    allowed_schemes = ("https", "http") if policy.allow_http else ("https",)
    if parts.scheme.lower() not in allowed_schemes:
        raise PermissionDeniedError(f"受控浏览器禁止访问该协议：{parts.scheme or 'unknown'}")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise InvalidRequestError("目标 URL 缺少主机名")
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local") or host.endswith(".internal"):
        raise PermissionDeniedError(f"受控浏览器禁止访问本机或内网主机：{host}")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise PermissionDeniedError(f"受控浏览器禁止访问私有/保留 IP（SSRF 防护）：{host}")
    except ValueError:
        pass
    if any(host == p or host.endswith("." + p) for p in _PAYMENT_HOST_SUFFIXES) or _PAYMENT_PATH_RE.search(parts.path):
        raise PermissionDeniedError("检测到支付/结账页面，受控浏览器禁止自动介入，请员工亲自在浏览器完成")
    if not policy.allowed_domains:
        raise PermissionDeniedError("当前未配置受控浏览器域名白名单，请联系管理员放行目标域名")
    if not any(host == d.lower() or host.endswith("." + d.lower()) for d in policy.allowed_domains):
        raise PermissionDeniedError(f"目标域名不在受控浏览器白名单内：{host}")
    return cleaned


def detect_page_block(snap: BrowserSnapshot) -> Literal["blocked_captcha", "blocked_payment"] | None:
    """检测目标页面是否命中人机验证或支付门禁；命中必须立即停机交还用户。"""
    parts = urlsplit(snap.final_url)
    host = (parts.hostname or "").lower()
    if any(host == p or host.endswith("." + p) for p in _PAYMENT_HOST_SUFFIXES) or _PAYMENT_PATH_RE.search(parts.path):
        return "blocked_payment"
    if _CAPTCHA_RE.search(snap.title) or _CAPTCHA_RE.search(snap.text[:4000]):
        return "blocked_captcha"
    return None


class BrowserSandboxBackendPlugin:
    """L2 ``isolated_worker`` 受控无头浏览器插件（严禁直接访问宿主 ``Platform`` 私有对象）。"""

    manifest = BackendPluginManifest(
        plugin_id="browser-sandbox",
        version="1.0.0",
        display_name="受控无头浏览器",
        description="带 SSRF 防护、域名白名单、CAPTCHA/支付硬停机、写操作审批与污点截图产物的受控浏览器插件。",
        trust_level="isolated_worker",
        permissions=("browser.read", "browser.interact", "artifact.publish"),
    )

    def __init__(
        self,
        *,
        policy: BrowserPolicy | None = None,
        driver: BrowserDriver | None = None,
        artifacts: TraceArtifactService | None = None,
    ) -> None:
        """仅注入最小作用域依赖（策略、浏览器驱动与过程产物服务）。"""
        self.policy = policy or BrowserPolicy()
        self._driver: BrowserDriver = driver or PlaywrightBrowserDriver()
        self._artifacts = artifacts
        self._mounted = False

    async def mount(self, ctx: PluginContext) -> None:
        """注册受控浏览路由与管家工具（不访问 ctx.platform，严守 isolated_worker 隔离边界）。"""
        self._mounted = True
        ctx.add_route("GET", "policy", self._get_policy)
        ctx.add_route("POST", "read", self._read)
        ctx.add_route("POST", "interact", self._interact)
        ctx.add_tool(
            "read_page",
            "在受控域名白名单内只读抓取网页正文与截图（严守 SSRF/CAPTCHA/支付停机红线）。",
            {"url": {"type": "string"}, "trace_id": {"type": ["string", "null"]}},
            self._tool_read,
            required=("url",),
        )

    async def health(self) -> PluginHealth:
        """返回插件挂载与策略状态。"""
        return PluginHealth(
            ok=self._mounted,
            detail=f"allowed_domains={len(self.policy.allowed_domains)}",
            checked_at=datetime.now(UTC),
        )

    async def unmount(self, *, drain_timeout: float = 15.0) -> None:
        """关闭浏览器驱动并标记已卸载。"""
        _ = drain_timeout
        self._mounted = False
        await self._driver.close()

    async def _get_policy(self, _who: Principal, _sub: str, _payload: dict[str, Any]) -> dict[str, Any]:
        return self.policy.model_dump(mode="json")

    async def _publish_screenshot(
        self, who: Principal, trace_id: str | None, png: bytes | None,
    ) -> str | None:
        """若提供了 trace_id 与截图字节，以污点只读产物入库供管家视觉直读与前端预览。"""
        if not png or not trace_id or self._artifacts is None:
            return None
        owner = TraceOwner(tenant_id=who.require_tenant(), user_id=who.user_id or who.actor)
        art = await self._artifacts.publish(
            owner,
            trace_id=trace_id,
            name="browser_capture.png",
            content=png,
            mime_type="image/png",
            contains_external_input=True,
        )
        return art.artifact_id

    async def _read(self, who: Principal, _sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = validate_browser_url(str(payload.get("url") or ""), self.policy)
        snap = await self._driver.read_page(url, timeout_seconds=self.policy.timeout_seconds)
        # 重定向落地地址再次走 SSRF + 白名单复检
        validate_browser_url(snap.final_url, self.policy)
        block = detect_page_block(snap)
        if block is not None:
            return {
                "status": block,
                "final_url": snap.final_url,
                "message": "命中人机验证或支付门禁，已按安全红线立即停止，请员工亲自处理。",
                "contains_external_input": True,
            }
        trace_id = str(payload.get("trace_id") or "").strip() or None
        artifact_id = await self._publish_screenshot(who, trace_id, snap.screenshot_png)
        return {
            "status": "ok",
            "final_url": snap.final_url,
            "title": snap.title[:200],
            "text": snap.text[: self.policy.max_text_chars],
            "screenshot_artifact_id": artifact_id,
            "contains_external_input": True,
        }

    async def _interact(self, who: Principal, _sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = validate_browser_url(str(payload.get("url") or ""), self.policy)
        actions = payload.get("actions")
        if not isinstance(actions, list) or not actions:
            raise InvalidRequestError("interact 必须提供非空 actions 列表")
        approved = bool(payload.get("approved") is True and str(payload.get("approval_id") or "").strip())
        if not approved:
            return {
                "status": "approval_required",
                "url": url,
                "action_count": len(actions),
                "message": "网页写入/提交交互属于外部副作用，需员工明确批准（approved=true + approval_id）后方可执行。",
            }
        snap = await self._driver.interact_page(url, actions, timeout_seconds=self.policy.timeout_seconds)
        validate_browser_url(snap.final_url, self.policy)
        block = detect_page_block(snap)
        if block is not None:
            return {
                "status": block,
                "final_url": snap.final_url,
                "message": "交互后命中人机验证或支付门禁，已立即停止交还员工。",
                "contains_external_input": True,
            }
        trace_id = str(payload.get("trace_id") or "").strip() or None
        artifact_id = await self._publish_screenshot(who, trace_id, snap.screenshot_png)
        return {
            "status": "ok",
            "final_url": snap.final_url,
            "title": snap.title[:200],
            "text": snap.text[: self.policy.max_text_chars],
            "screenshot_artifact_id": artifact_id,
            "contains_external_input": True,
        }

    async def _tool_read(self, who: Principal, args: dict[str, Any]) -> dict[str, Any]:
        return await self._read(who, "read", args)
