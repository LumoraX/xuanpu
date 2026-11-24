"""桌面客户端的浏览器接力登录（ADR 0021）。

桌面客户端在自己的 WebView 里开始登录，飞书授权却常由飞书 App 或系统浏览器接手，回调落在系统
浏览器、到不了 WebView。接力把两边接起来::

    WebView  GET /v1/auth/feishu/login?client=desktop
             → 写登录尝试 cookie（只在 WebView 里），返回等待页：显示确认码，链接在系统浏览器打开授权页
    浏览器   GET /v1/auth/feishu/callback        → 认出员工，建接力记录（绑定 cookie 摘要），显示确认页
    浏览器   POST /v1/auth/feishu/desktop/confirm → 员工核对确认码一致后确认
    WebView  GET /v1/auth/feishu/desktop/status   → 等待页轮询：pending / confirm / ready
    WebView  GET /v1/auth/feishu/desktop/redeem   → 取出已确认的接力记录，签发登录会话，302 到 return_to

**安全**：换取必须出示登录尝试 cookie，它只在发起登录的 WebView 里，不出现在任何链接中；确认串
只在浏览器确认页的表单里。两边库里都只存摘要。把授权链接转发给别人、诱骗对方授权的，还要对方
在确认页上点「确认登录」——确认页写明确认码须与本人客户端显示的一致，不是本人发起的不要确认。
接力记录 ``identity.handoff.HANDOFF_TTL_SECONDS`` 内有效，确认与换取各只能一次。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Literal

from server.domains.identity.cookies import login_token_hash
from server.domains.identity.login import LoginFailedError, LoginRedirect

if TYPE_CHECKING:
    from collections.abc import Callable

    from server.domains.identity.login import FeishuLogin
    from server.domains.identity.models import DesktopHandoff
    from server.domains.identity.store import IdentityStore

logger = logging.getLogger(__name__)

MAX_NONCE_LENGTH = 64
"""确认串的最大长度（``secrets.token_urlsafe(32)`` 是 43 个字符）。"""
HandoffStatus = Literal["pending", "confirm", "ready", "invalid"]
"""等待页轮询的结果：还没回调、等员工在浏览器确认、可以换取、凭据缺失。"""


class DesktopLogin:
    """接力的确认、轮询与换取。由 ``IdentityService`` 组合，接入层经 ``IdentityService.desktop`` 调用。"""

    def __init__(
        self,
        *,
        store: IdentityStore,
        login: FeishuLogin,
        now: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            store: 身份存储。
            login: 飞书登录流程（复用它的租户检查、签发登录会话与失败处理）。
            now: 取当前时间的函数，测试时可替换。
        """
        self._store = store
        self._login = login
        self._now = now

    async def confirm(self, nonce: str | None) -> bool:
        """员工在浏览器里确认；确认串缺失、形状不对、已用过或过期返回 False。"""
        if not nonce or len(nonce) > MAX_NONCE_LENGTH:
            return False
        handoff = await self._store.confirm_desktop_handoff(
            login_token_hash(nonce), now=self._now()
        )
        if handoff is None:
            return False
        logger.info("桌面接力已在浏览器确认（租户 %s）", handoff.tenant_id)
        return True

    async def status(self, attempt_cookie: str | None) -> HandoffStatus:
        """等待页轮询：按 WebView 带来的登录尝试 cookie 查接力进度。只读。"""
        if attempt_cookie is None:
            return "invalid"
        handoff = await self._store.get_desktop_handoff(
            login_token_hash(attempt_cookie), now=self._now()
        )
        if handoff is None:
            return "pending"
        return "ready" if handoff.confirmed else "confirm"

    async def redeem(
        self, attempt_cookie: str | None, session_cookie: str | None = None
    ) -> LoginRedirect:
        """换取：取出已确认的接力记录，签发登录会话，跳去 ``return_to``；失败跳回落地页。"""
        try:
            handoff = await self._take(attempt_cookie)
            tenant_id = await self._login.active_tenant(handoff.tenant_id)
            user = await self._store.get_user(tenant_id, handoff.user_id)
            if user is None or user.status != "active":
                raise LoginFailedError(
                    "user_disabled", tenant_id=tenant_id, user_id=handoff.user_id
                )
            config = await self._login.tenant_config(tenant_id)
            token, ttl = await self._login.issue_session(user, config, client="desktop")
        except LoginFailedError as failure:
            return await self._login.failed(failure)
        await self._login.revoke(session_cookie)
        return LoginRedirect(handoff.return_to, session_token=token, session_max_age=ttl)

    async def _take(self, attempt_cookie: str | None) -> DesktopHandoff:
        """取出已确认的接力记录；没有 cookie、没有记录或还没确认都按 ``state_invalid``。"""
        if attempt_cookie is None:
            raise LoginFailedError("state_invalid", detail="换取时没有登录尝试 cookie")
        handoff = await self._store.take_desktop_handoff(
            login_token_hash(attempt_cookie), now=self._now()
        )
        if handoff is None:
            raise LoginFailedError("state_invalid", detail="桌面接力不存在、未确认、已用过或已过期")
        return handoff


__all__ = ["MAX_NONCE_LENGTH", "DesktopLogin", "HandoffStatus"]
