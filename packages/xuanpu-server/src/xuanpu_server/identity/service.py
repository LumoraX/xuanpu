"""身份服务：把请求里的登录 cookie、代员工调用的请求头解析成调用者（设计 6.2、6.3）。

登录的开始与完成、停用流水线、对账、用户管理以后也收在这里（P1 后续任务）；本模块目前只有
认证相关的两块：

- ``authenticate_login``：浏览器登录。**每个请求都查库**——登录会话存在且未过期、员工启用、
  租户启用，三者任一不满足都按凭据无效处理（接入层另外清掉 cookie）。不做缓存，停用立即生效。
  ``last_seen_at`` 最多每 5 分钟写一次，免得每个请求都写库。
- ``act_as``：系统代员工调用。key 必须在签发时被授予这项能力；员工必须属于 key 的租户、且为
  启用状态；生效角色取 key 与员工两者中较低的那个。

两处都遵守身份存储的约定：``get_login_session`` 不带租户，租户一律取自返回的登录会话记录，
再拿它去查员工与租户。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from xuanpu_server.errors import LoginSessionInvalidError, PermissionDeniedError
from xuanpu_server.identity.cookies import login_token_hash
from xuanpu_server.tenancy import Principal, lower_role

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.identity.models import UserRecord
    from xuanpu_server.identity.store import IdentityStore
    from xuanpu_server.tenancy import ApiKeyRecord

LAST_SEEN_INTERVAL_SECONDS = 300.0
"""登录会话的 ``last_seen_at`` 最多隔这么久写一次。"""


@dataclass(frozen=True)
class IdentitySettings:
    """身份域的平台级设置，来自服务配置的 ``public_url`` 与 ``identity`` 段（设计第 11 节）。

    Attributes:
        public_url: 平台对外地址，配置加载时已规整为源（``scheme://host[:port]``，不带路径）；
            None 表示没有配置。用来校验 cookie 认证请求的 ``Origin``（见 ``identity.cookies``），
            以后也用来拼飞书的回调地址与事件地址（``<public_url>/v1/auth/feishu/callback`` 等）。
        session_ttl_seconds: 登录会话的默认有效期；租户的身份配置可以覆盖。
        cookie_secure: 登录 cookie 是否只走 HTTPS。
        reconcile_interval_seconds: 对账周期。
        default_tenant: 登录链接省略 ``tenant`` 时用的租户。
        login_landing_path: 登录完成或失败后的默认落地页（站内路径）。
        feishu_open_base: 飞书开放接口的基地址。
        feishu_accounts_base: 飞书授权页的基地址。
    """

    public_url: str | None = None
    session_ttl_seconds: int = 604800
    cookie_secure: bool = True
    reconcile_interval_seconds: float = 86400.0
    default_tenant: str | None = None
    login_landing_path: str = "/console/"
    feishu_open_base: str = "https://open.feishu.cn"
    feishu_accounts_base: str = "https://accounts.feishu.cn"


class IdentityService:
    """身份服务。"""

    def __init__(
        self,
        *,
        store: IdentityStore,
        control_plane: ControlPlaneStore,
        settings: IdentitySettings,
        now: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            store: 身份存储。
            control_plane: 同一部署的控制面存储（查租户是否启用）。
            settings: 身份域的平台级设置。
            now: 取当前时间的函数，测试时可替换。
        """
        self.store = store
        self.settings = settings
        self._control_plane = control_plane
        self._now = now

    async def authenticate_login(self, token: str) -> Principal:
        """由登录 cookie 里的令牌得到调用者（浏览器登录）。

        Raises:
            LoginSessionInvalidError: 会话不存在或已过期、员工已停用、租户已停用。对外与凭据
                无效相同，不透露是哪一步没过。
        """
        now = self._now()
        token_hash = login_token_hash(token)
        session = await self.store.get_login_session(token_hash, now=now)
        if session is None:
            raise LoginSessionInvalidError("凭据无效")
        # 这次查询不带租户：之后一律用会话记录里的租户
        tenant_id = session.tenant_id
        user = await self.store.get_user(tenant_id, session.user_id)
        if user is None or user.status != "active":
            raise LoginSessionInvalidError("凭据无效")
        tenant = await self._control_plane.get_tenant(tenant_id)
        if tenant is None or tenant.status != "active":
            raise LoginSessionInvalidError("凭据无效")
        if now - session.last_seen_at >= LAST_SEEN_INTERVAL_SECONDS:
            await self.store.touch_login_session(tenant_id, token_hash, at=now)
        return Principal(role=user.role, tenant_id=tenant_id, key_id=None, user_id=user.user_id)

    async def act_as(self, key: ApiKeyRecord, user_id: str) -> Principal:
        """一把已认证的 API key 代某名员工调用，返回生效的调用者。

        key 所在租户是否启用，由认证 key 的那一步查过。员工不存在、属于别的租户、已停用，
        对外是同一句提示，不透露这个标识在别的租户里是否存在。

        Raises:
            PermissionDeniedError: key 没有代员工调用的能力，或本租户没有这名启用的员工。
        """
        if not key.can_act_as_user:
            raise PermissionDeniedError(
                "这把 API key 没有代员工调用的能力（签发时未授予 act_as_users）"
            )
        user = await self.store.get_user(key.tenant_id, user_id)
        if user is None or user.status != "active":
            raise PermissionDeniedError("不能代这名员工调用：本租户没有这名启用的员工")
        return Principal(
            role=lower_role(key.role, user.role),
            tenant_id=key.tenant_id,
            key_id=key.key_id,
            user_id=user.user_id,
        )

    async def current_user(self, principal: Principal) -> UserRecord | None:
        """调用者代表的员工；不代表员工的（API key、根凭据）返回 None。"""
        if principal.user_id is None:
            return None
        tenant_id = principal.require_tenant()
        return await self.store.get_user(tenant_id, principal.user_id)


__all__ = ["LAST_SEEN_INTERVAL_SECONDS", "IdentityService", "IdentitySettings"]
