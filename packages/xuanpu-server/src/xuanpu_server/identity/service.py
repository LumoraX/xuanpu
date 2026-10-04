"""身份服务：认出调用者（设计 6.2、6.3）、飞书登录与退出（7.1）、停用流水线（7.2）、离职事件回调
（7.3）、用户管理与租户的飞书配置（第 9 节）。

飞书登录、离职事件与通讯录对账统一在这里装配：

- ``authenticate_login``：浏览器登录。**每个请求都查库**——登录会话存在且未过期、员工启用、
  租户启用，三者任一不满足都按凭据无效处理（接入层另外清掉 cookie）。不做缓存，停用立即生效。
  ``last_seen_at`` 最多每 5 分钟写一次，免得每个请求都写库。
- ``act_as``：系统代员工调用。key 必须在签发时被授予这项能力；员工必须属于 key 的租户、且为
  启用状态；生效角色取 key 与员工两者中较低的那个。
- ``login``：飞书登录的流程（``identity.login.FeishuLogin``）；``logout`` 删掉当前登录会话。
- ``maintenance``：周期任务（``identity.maintenance``），清理过期的登录尝试与登录会话；随平台的
  ``start``、``close`` 启停，同时触发已绑定租户的通讯录对账。
- ``feishu_config``、``put_feishu_config``：租户管理员读写本租户的飞书应用配置。
- ``disabling``：员工停用流水线（``identity.disable``），「员工停用」监听者由装配处登记；
  ``disable_user``、``enable_user`` 是它与启用的入口。
- ``users``：用户管理（``identity.users``）；``events``：飞书事件回调（``identity.callback``）。

两处认证都遵守身份存储的约定：``get_login_session`` 不带租户，租户一律取自返回的登录会话记录，
再拿它去查员工与租户。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from xuanpu_server.errors import (
    AuthenticationError,
    InvalidRequestError,
    LoginSessionInvalidError,
    PermissionDeniedError,
    PlatformError,
    SecretMissingError,
)
from xuanpu_server.identity.callback import FeishuEventCallback
from xuanpu_server.identity.config import normalize_origin
from xuanpu_server.identity.cookies import login_token_hash
from xuanpu_server.identity.disable import DisablePipeline
from xuanpu_server.identity.login import FEISHU, FeishuLogin
from xuanpu_server.identity.maintenance import IdentityMaintenance
from xuanpu_server.identity.models import TenantIdentityConfig
from xuanpu_server.identity.reconcile import IdentityReconcile
from xuanpu_server.identity.users import UserAdmin
from xuanpu_server.secrets import validate_secret_name
from xuanpu_server.tenancy import Principal, lower_role

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.identity.config import IdentityConfig
    from xuanpu_server.identity.disable import DisableOutcome
    from xuanpu_server.identity.feishu import FeishuClient
    from xuanpu_server.identity.models import DisabledReason, UserRecord
    from xuanpu_server.identity.store import IdentityStore
    from xuanpu_server.metrics import PlatformMetrics
    from xuanpu_server.secrets import SecretVault
    from xuanpu_server.tenancy import ApiKeyRecord

logger = logging.getLogger(__name__)

LAST_SEEN_INTERVAL_SECONDS = 300.0
"""登录会话的 ``last_seen_at`` 最多隔这么久写一次。"""


@dataclass(frozen=True)
class IdentitySettings:
    """身份域的平台级设置，来自服务配置的 ``public_url`` 与 ``identity`` 段（设计第 11 节）。

    各项没有默认值：默认值只在 ``IdentityConfig`` 里写一份，这里一律经 ``from_config`` 构造。

    Attributes:
        public_url: 平台对外地址，已规整为源（``scheme://host[:port]``，不带路径）；None 表示
            没有配置，这时平台不认登录 cookie（见 ``identity.cookies``），也不能启用飞书登录。
            配置了就用来校验 cookie 认证请求的 ``Origin``，拼飞书的回调地址与事件地址
            （``<public_url>/v1/auth/feishu/callback`` 等）。
        session_ttl_seconds: 登录会话的默认有效期；租户的身份配置可以覆盖。
        cookie_secure: 登录 cookie 是否只走 HTTPS。
        reconcile_interval_seconds: 对账周期。
        default_tenant: 登录链接省略 ``tenant`` 时用的租户。
        login_landing_path: 登录完成或失败后的默认落地页（站内路径）。
        feishu_open_base: 飞书开放接口的基地址。
        feishu_accounts_base: 飞书授权页的基地址。
    """

    public_url: str | None
    session_ttl_seconds: int
    cookie_secure: bool
    reconcile_interval_seconds: float
    default_tenant: str | None
    login_landing_path: str
    feishu_open_base: str
    feishu_accounts_base: str

    @classmethod
    def from_config(cls, public_url: str | None, section: IdentityConfig) -> IdentitySettings:
        """由服务配置的 ``public_url`` 与 ``identity`` 段构造。

        ``public_url`` 再规整一次（幂等）：不经服务配置直接构造时（测试）也是规整后的源。
        """
        origin = None if public_url is None else normalize_origin(public_url, name="public_url")
        return cls(
            public_url=origin,
            session_ttl_seconds=section.session_ttl_seconds,
            cookie_secure=section.cookie_secure,
            reconcile_interval_seconds=section.reconcile_interval_seconds,
            default_tenant=section.default_tenant,
            login_landing_path=section.login_landing_path,
            feishu_open_base=section.feishu_open_base,
            feishu_accounts_base=section.feishu_accounts_base,
        )


class IdentityService:
    """身份服务。"""

    def __init__(
        self,
        *,
        store: IdentityStore,
        control_plane: ControlPlaneStore,
        settings: IdentitySettings,
        vault: SecretVault,
        audit: AuditLog,
        feishu: FeishuClient,
        metrics: PlatformMetrics | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            store: 身份存储。
            control_plane: 同一部署的控制面存储（查租户是否启用）。
            settings: 身份域的平台级设置。
            vault: 密钥保管库（飞书的 App Secret；配置引用的密钥是否存在）。
            audit: 审计写入口。
            feishu: 飞书接口的客户端；归本服务所有，``close`` 时关闭。
            metrics: 平台指标；None 不打点。
            now: 取当前时间的函数，测试时可替换。
        """
        self.store = store
        self.settings = settings
        self._control_plane = control_plane
        self._vault = vault
        self._audit = audit
        self._now = now
        self.login = FeishuLogin(
            store=store,
            control_plane=control_plane,
            vault=vault,
            audit=audit,
            settings=settings,
            feishu=feishu,
            metrics=metrics,
            now=now,
        )
        self.disabling = DisablePipeline(store=store, audit=audit, metrics=metrics, now=now)
        self.users = UserAdmin(store=store, disabling=self.disabling, audit=audit, now=now)
        self.events = FeishuEventCallback(
            store=store, vault=vault, disabling=self.disabling, metrics=metrics
        )

        self.reconcile = IdentityReconcile(
            self,
            control_plane=control_plane,
            vault=vault,
            audit=audit,
            feishu=feishu,
            metrics=metrics,
            now=now,
        )
        self.maintenance = IdentityMaintenance(store, reconcile=self.reconcile.run_due, now=now)

    def start(self) -> None:
        """启动身份域的周期任务（清理过期的登录尝试与登录会话）。"""
        self.maintenance.start()

    async def close(self) -> None:
        """先停周期任务（等它结束），再等停用流水线的后台收尾（有上限，见 ``identity.disable``），
        再关闭飞书客户端的连接。可以重复调用。"""
        await self.maintenance.close()
        await self.disabling.close()
        await self.login.feishu.aclose()

    async def disable_user(
        self,
        tenant_id: str,
        user_id: str,
        reason: DisabledReason,
        actor: Principal,
        *,
        keep_an_admin: bool,
    ) -> DisableOutcome:
        """停用员工：停用流水线的三步都做完再返回（``identity.disable``）。可以重复调用：对已停用的
        员工状态不变、不再记 ``user_disabled``，但会重跑全部监听者。

        Args:
            actor: 谁停用的：管理员接口取调用者，飞书事件、对账取系统调用者。
            keep_an_admin: 为真时不停用最后一名启用的管理员（管理员接口）；离职事件、对账为假。

        Raises:
            NotFoundError: 本租户没有这名员工。
            ConflictError: ``keep_an_admin`` 为真且这是最后一名启用的管理员。
        """
        return await self.disabling.disable(
            tenant_id, user_id, reason, actor, keep_an_admin=keep_an_admin
        )

    async def enable_user(self, tenant_id: str, user_id: str, actor: Principal) -> UserRecord:
        """重新启用员工：只改状态、记 ``user_enabled``，不恢复已关闭的会话（``identity.users``）。

        Raises:
            NotFoundError: 本租户没有这名员工。
        """
        return await self.users.enable(tenant_id, user_id, actor)

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

    async def logout(self, principal: Principal, token: str | None) -> None:
        """退出浏览器登录：删掉这个登录 cookie 对应的登录会话，记 ``logout``。

        Args:
            principal: 调用者；必须是浏览器登录，即凭 ``token`` 这个登录 cookie 认出来的。
            token: 请求里登录 cookie 的令牌。

        Raises:
            InvalidRequestError: 调用者不是浏览器登录（API key、代员工调用、根凭据）。
            AuthenticationError: 请求里没有登录 cookie（浏览器登录一定是凭它认出来的，走到这里
                说明认证与取 cookie 不一致，按凭据无效处理）。
        """
        if principal.key_id is not None or principal.user_id is None:
            raise InvalidRequestError("退出登录只用于浏览器登录（登录 cookie）；API key 不需要退出")
        if token is None:
            raise AuthenticationError("凭据无效")
        tenant_id = principal.require_tenant()
        await self.store.delete_login_session(tenant_id, login_token_hash(token))
        await self._audit.control(principal, tenant_id, "logout")

    async def feishu_config(self, principal: Principal) -> TenantIdentityConfig | None:
        """本租户的飞书配置（只有密钥的名字，没有明文）；还没配置返回 None。租户管理员可读。"""
        tenant_id = principal.require_tenant_admin()
        return await self.store.get_identity_config(tenant_id, FEISHU)

    async def put_feishu_config(
        self,
        principal: Principal,
        *,
        app_id: str,
        app_secret_ref: str,
        encrypt_key_ref: str | None = None,
        verification_token_ref: str | None = None,
        session_ttl_seconds: int | None = None,
    ) -> TenantIdentityConfig:
        """写入或整体替换本租户的飞书配置（已绑定的飞书企业保留），返回写入后的配置。

        App Secret 必填，事件的两个引用可单独保存，只有齐备时启用事件。
        密钥只收名字，明文由管理员先经 ``PUT /v1/secrets/{name}`` 写进保管库。审计
        ``identity_config_updated`` 只记 App ID、有效期与三个名字。

        Raises:
            PermissionDeniedError: 不是租户管理员。
            InvalidRequestError: 平台没有配置 ``public_url``（启用飞书登录必须有），或密钥名不合法。
            SecretMissingError: 引用的密钥还不在保管库里。
        """
        tenant_id = principal.require_tenant_admin()
        if self.settings.public_url is None:
            raise InvalidRequestError(
                "平台没有配置 public_url：飞书的回调地址与事件地址由它生成，启用飞书登录前先配置它"
            )
        encrypt_key_ref = None if encrypt_key_ref == "" else encrypt_key_ref
        verification_token_ref = None if verification_token_ref == "" else verification_token_ref
        refs = [app_secret_ref]
        refs.extend(name for name in (encrypt_key_ref, verification_token_ref) if name is not None)
        for name in refs:
            validate_secret_name(name)
        present = set(await self._vault.names(tenant_id))
        missing = [name for name in refs if name not in present]
        if missing:
            listed = "、".join(missing)
            raise SecretMissingError(
                f"引用的密钥还不在保管库里：{listed}（先用 PUT /v1/secrets/{{name}} 写入）"
            )
        await self.store.put_identity_config(
            TenantIdentityConfig(
                tenant_id=tenant_id,
                provider=FEISHU,
                app_id=app_id,
                app_secret_ref=app_secret_ref,
                encrypt_key_ref=encrypt_key_ref,
                verification_token_ref=verification_token_ref,
                session_ttl_seconds=session_ttl_seconds,
            )
        )
        # 键名避开 secret、token 这类词：审计的按键名脱敏会把它们的取值（这里只是名字）抹掉
        names = {
            "app": app_secret_ref,
            "encrypt": encrypt_key_ref,
            "verification": verification_token_ref,
        }
        await self._audit.control(
            principal,
            tenant_id,
            "identity_config_updated",
            {"app_id": app_id, "session_ttl_seconds": session_ttl_seconds, "refs": names},
        )
        stored = await self.store.get_identity_config(tenant_id, FEISHU)
        if stored is None:
            # 刚写进去就读不回来（配置不会删除）：存储出了问题，按内部错误处理，细节只进日志
            logger.error("租户 %s 的飞书配置刚写入，身份存储里却读不回来", tenant_id)
            raise PlatformError("服务内部错误")
        return stored


__all__ = ["LAST_SEEN_INTERVAL_SECONDS", "IdentityService", "IdentitySettings"]
