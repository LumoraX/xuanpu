"""玄圃自有账号（ADR 0019）：管理员开通账号与签发设置链接、员工设口令、用户名口令登录、改口令。

::

    管理员 POST /v1/users                      → 建员工 + 开通用户名（口令待设）+ 设置链接
    管理员 POST /v1/users/{id}/password-link   → 设置链接（没开通的先开通；已有口令的是重置）
    员工   POST /v1/auth/password/setup/inspect → 看链接是给谁的（不消耗链接）
    员工   POST /v1/auth/password/setup         → 兑现链接、设口令、直接登录
    员工   POST /v1/auth/password/login         → 用户名口令登录
    员工   POST /v1/me/password                 → 改口令（核对旧口令），其他登录全部退出

登录成功签发与飞书登录同一种服务端登录会话（ADR 0010 决策 4），停用、退出、过期规则相同。

**失败不透露原因**：用户名不存在、没设口令、口令错、账号锁定、员工或租户停用，对外都是同一个
``401``「用户名或口令不正确」；不存在的用户名照样算一次摘要（``PasswordHashing.verify_absent``）。
同一账号连续失败 ``LOCK_AFTER_FAILURES`` 次锁 ``LOCK_SECONDS`` 秒；锁定期内不核对真口令。只有认出
了员工的失败才记 ``login_failed`` 审计（与飞书登录同一口径，匿名请求刷不出审计）。

**设置链接**：令牌 32 字节随机数，库里只存摘要；开通 7 天有效，重置 24 小时；每名员工同时只有一
条，新签发的替换旧的，设了口令后全部作废。链接形如 ``<public_url><落地页>#password_setup=<令牌>``：
令牌在片段里，浏览器不会把它发给服务端，也不进 Referer。兑现时先查链接、再算摘要、最后原子地删
链接并写口令，同一条链接并发兑现至多一个成功。重置成功后这名员工已有的登录会话全部作废。
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from server.core.errors import (
    AuthenticationError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
)
from server.core.tenancy import validate_tenant_id
from server.domains.identity.cookies import login_token_hash, new_login_token
from server.domains.identity.credential_models import (
    AccountToken,
    LocalCredential,
    normalize_username,
)
from server.domains.identity.models import LoginSession, UserRecord, new_user_id
from server.domains.identity.passwords import validate_new_password

if TYPE_CHECKING:
    from collections.abc import Callable

    from server.core.audit import AuditLog
    from server.core.controlplane.store import ControlPlaneStore
    from server.core.metrics import PasswordLoginResult, PlatformMetrics
    from server.core.tenancy import Principal, TenantRole
    from server.domains.identity.credential_models import AccountTokenPurpose
    from server.domains.identity.passwords import PasswordHashing
    from server.domains.identity.service import IdentitySettings
    from server.domains.identity.store import IdentityStore

logger = logging.getLogger(__name__)

PASSWORD = "password"  # noqa: S105 —— 登录方式的名字，不是口令
"""审计与登录方式里口令登录的名字。"""
LOCK_AFTER_FAILURES = 5
"""连续失败多少次锁定。"""
LOCK_SECONDS = 15 * 60.0
"""锁定多久（秒）。"""
SETUP_LINK_TTL_SECONDS = 7 * 86400
"""开通设口令的链接有效期：7 天。"""
RESET_LINK_TTL_SECONDS = 86400
"""重置口令的链接有效期：24 小时。"""
SETUP_FRAGMENT = "password_setup"
"""设置链接片段里的参数名。"""
REJECTED = "用户名或口令不正确"
"""登录失败对外的唯一提示。"""
LINK_INVALID = "设置链接无效或已过期，请联系管理员重新签发"
"""设置链接不能用时对外的唯一提示。"""


@dataclass(frozen=True)
class SetupLink:
    """签发的设置链接。``url`` 交给管理员转给员工，只在签发时返回一次。"""

    url: str = field(repr=False)
    user_id: str
    username: str
    purpose: AccountTokenPurpose
    expires_at: float


@dataclass(frozen=True)
class SetupLinkInfo:
    """设置链接是给谁的：设口令页据此显示用户名。"""

    tenant_id: str
    username: str
    display_name: str
    purpose: AccountTokenPurpose
    expires_at: float


@dataclass(frozen=True)
class SignedIn:
    """登录成功：接入层据此写登录 cookie。"""

    session_token: str = field(repr=False)
    max_age: int
    tenant_id: str
    user_id: str


class LocalAccounts:
    """自有账号的流程。由 ``IdentityService`` 组合，接入层经 ``IdentityService.accounts`` 调用。"""

    def __init__(  # noqa: PLR0913 —— 依赖逐项注入
        self,
        *,
        store: IdentityStore,
        control_plane: ControlPlaneStore,
        audit: AuditLog,
        settings: IdentitySettings,
        hashing: PasswordHashing,
        metrics: PlatformMetrics | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            store: 身份存储（凭据经 ``store.credentials``）。
            control_plane: 控制面存储（查租户是否启用）。
            audit: 审计写入口。
            settings: 身份域的平台级设置（对外地址、会话有效期、落地页、默认租户）。
            hashing: 口令摘要的计算与核对。
            metrics: 平台指标；None 不打点。
            now: 取当前时间的函数，测试时可替换。
        """
        self._store = store
        self._credentials = store.credentials
        self._control_plane = control_plane
        self._audit = audit
        self._settings = settings
        self.hashing = hashing
        self._metrics = metrics
        self._now = now

    # --- 管理员 ---

    async def create_account(
        self,
        principal: Principal,
        *,
        username: str,
        display_name: str,
        role: TenantRole = "tenant_member",
    ) -> tuple[UserRecord, SetupLink]:
        """开通一名员工：建员工、开通用户名（口令待设），返回员工与设置链接（租户管理员）。

        先查用户名是否被占用再建员工；两名管理员同时用同一个用户名开通时，后到的那个在开通凭据时
        冲突，已建的员工保留（没有登录方式），管理员可以给他另签一个用户名的链接。

        Raises:
            PermissionDeniedError: 不是租户管理员。
            InvalidRequestError: 用户名或显示名不合规则，或平台没有配置 ``public_url``。
            ConflictError: 用户名在本租户已被占用。
        """
        tenant_id = principal.require_tenant_admin()
        self._require_public_url()
        name = _username(username)
        display = display_name.strip()
        if not display:
            raise InvalidRequestError("显示名不能为空")
        if await self._credentials.find_credential(tenant_id, name) is not None:
            raise ConflictError(f"用户名 {name!r} 已被占用")
        now = self._now()
        user = UserRecord(
            tenant_id=tenant_id, user_id=new_user_id(), display_name=display, created_at=now,
            role=role,
        )  # fmt: skip
        await self._store.create_user(user)
        await self._credentials.create_credential(
            LocalCredential(
                tenant_id=tenant_id, user_id=user.user_id, username=name, created_at=now
            )
        )
        data = {"provider": PASSWORD, "username": name, "display_name": display, "role": role}
        await self._audit.control(principal, tenant_id, "user_created", data, user_id=user.user_id)
        return user, await self._issue(principal, user, name, "setup")

    async def issue_link(
        self, principal: Principal, user_id: str, *, username: str | None = None
    ) -> SetupLink:
        """给一名员工签发设置链接（租户管理员）。

        员工还没开通用户名的（比如飞书来的）必须给 ``username``，在这里开通；已开通的不能改用户名，
        ``username`` 省略或与原来相同。已设过口令的是重置链接，否则是开通链接。

        Raises:
            PermissionDeniedError: 不是租户管理员。
            NotFoundError: 本租户没有这名员工。
            InvalidRequestError: 用户名不合规则、需要却没给、想改用户名，或没有配置 ``public_url``。
            ConflictError: 员工已停用，或用户名已被别人占用。
        """
        tenant_id = principal.require_tenant_admin()
        self._require_public_url()
        user = await self._store.get_user(tenant_id, user_id)
        if user is None:
            raise NotFoundError(f"用户 {user_id!r} 不存在")
        if user.status != "active":
            raise ConflictError("员工已停用，不能签发设置链接；先启用再签发")
        credential = await self._credentials.get_credential(tenant_id, user_id)
        if credential is None:
            if username is None:
                raise InvalidRequestError("这名员工还没有用户名，签发设置链接时必须给 username")
            name = _username(username)
            credential = LocalCredential(
                tenant_id=tenant_id, user_id=user_id, username=name, created_at=self._now()
            )
            await self._credentials.create_credential(credential)
            await self._audit.control(
                principal, tenant_id, "username_assigned", {"username": name}, user_id=user_id
            )
        elif username is not None and _username(username) != credential.username:
            raise InvalidRequestError("已开通的用户名暂不支持修改")
        purpose: AccountTokenPurpose = "reset" if credential.has_password else "setup"
        return await self._issue(principal, user, credential.username, purpose)

    async def _issue(
        self, principal: Principal, user: UserRecord, username: str, purpose: AccountTokenPurpose
    ) -> SetupLink:
        """签发一条设置链接（替换这名员工已有的），记 ``password_link_issued``。"""
        now = self._now()
        ttl = SETUP_LINK_TTL_SECONDS if purpose == "setup" else RESET_LINK_TTL_SECONDS
        token = secrets.token_urlsafe(32)
        record = AccountToken(
            token_hash=login_token_hash(token), tenant_id=user.tenant_id, user_id=user.user_id,
            purpose=purpose, created_at=now, expires_at=now + ttl, issued_by=principal.actor,
        )  # fmt: skip
        await self._credentials.put_account_token(record)
        await self._audit.control(
            principal, user.tenant_id, "password_link_issued",
            {"purpose": purpose, "expires_at": record.expires_at}, user_id=user.user_id,
        )  # fmt: skip
        public_url = self._require_public_url()
        url = f"{public_url}{self._settings.login_landing_path}#{SETUP_FRAGMENT}={token}"
        return SetupLink(url, user.user_id, username, purpose, record.expires_at)

    # --- 员工：设置链接 ---

    async def inspect_link(self, token: str) -> SetupLinkInfo:
        """设置链接是给谁的（不消耗链接）。

        Raises:
            AuthenticationError: 链接无效、过期，或员工、租户已停用（同一句提示）。
        """
        record, user, credential = await self._usable_link(token)
        return SetupLinkInfo(
            tenant_id=record.tenant_id, username=credential.username,
            display_name=user.display_name, purpose=record.purpose, expires_at=record.expires_at,
        )  # fmt: skip

    async def redeem_link(self, token: str, password: str) -> SignedIn:
        """兑现设置链接：设口令，作废这名员工已有的登录会话，签发新的登录会话。

        Raises:
            AuthenticationError: 链接无效、过期、已被兑现，或员工、租户已停用（同一句提示）。
            InvalidRequestError: 口令不合规则（链接不消耗，可以换个口令再试）。
        """
        record, user, credential = await self._usable_link(token)
        validate_new_password(password, username=credential.username)
        password_hash = await self.hashing.hash(password)
        if not await self._credentials.redeem_account_token(
            record, password_hash, at=self._now()
        ):
            raise AuthenticationError(LINK_INVALID)
        await self._store.delete_user_login_sessions(user.tenant_id, user.user_id)
        await self._audit.login(
            user.tenant_id, "password_set", {"purpose": record.purpose}, user_id=user.user_id
        )
        return await self._sign_in(user)

    async def _usable_link(self, token: str) -> tuple[AccountToken, UserRecord, LocalCredential]:
        """按令牌找到仍然可用的链接、它的员工与凭据；任何一步不满足都是同一句提示。"""
        if not token or len(token) > 128:
            raise AuthenticationError(LINK_INVALID)
        record = await self._credentials.get_account_token(login_token_hash(token), now=self._now())
        if record is None:
            raise AuthenticationError(LINK_INVALID)
        # 这次查询不带租户：之后一律用链接记录里的租户
        user = await self._active_user(record.tenant_id, record.user_id)
        credential = await self._credentials.get_credential(record.tenant_id, record.user_id)
        if user is None or credential is None:
            raise AuthenticationError(LINK_INVALID)
        return record, user, credential

    # --- 员工：登录与改口令 ---

    async def login(
        self, *, tenant: str | None, username: str, password: str, previous: str | None = None
    ) -> SignedIn:
        """用户名口令登录；成功时浏览器带着的旧登录会话（``previous``）作废。

        Raises:
            AuthenticationError: 登录失败（原因不对外区分，见模块说明）。
            InvalidRequestError: 平台没有配置 ``public_url``（不签发登录 cookie）。
        """
        self._require_public_url()
        credential = await self._find(tenant, username)
        now = self._now()
        if credential is None or credential.password_hash is None:
            await self.hashing.verify_absent(password)
            self._count("rejected")
            raise AuthenticationError(REJECTED)
        if credential.locked(now):
            await self.hashing.verify_absent(password)
            await self._rejected(credential, "locked")
            raise AuthenticationError(REJECTED)
        matched, rehashed = await self.hashing.verify(credential.password_hash, password)
        if not matched:
            await self._failure(credential)
            raise AuthenticationError(REJECTED)
        user = await self._active_user(credential.tenant_id, credential.user_id)
        if user is None:
            await self._rejected(credential, "user_disabled")
            raise AuthenticationError(REJECTED)
        await self._credentials.record_login_success(
            credential.tenant_id, credential.user_id, rehashed=rehashed
        )
        await self._revoke(previous)
        return await self._sign_in(user)

    async def change_password(
        self, principal: Principal, *, current: str, new: str
    ) -> SignedIn:
        """员工改自己的口令：核对旧口令，写新口令，作废全部登录会话，签发新的登录会话。

        Raises:
            InvalidRequestError: 调用者不是浏览器登录的员工、没有开通口令，或新口令不合规则。
            AuthenticationError: 旧口令不对或账号锁定（计入连续失败）。
        """
        if principal.key_id is not None or principal.user_id is None:
            raise InvalidRequestError("改口令只能由员工本人在浏览器登录后操作")
        tenant_id = principal.require_tenant()
        credential = await self._credentials.get_credential(tenant_id, principal.user_id)
        if credential is None or credential.password_hash is None:
            raise InvalidRequestError("还没有开通用户名口令，请联系管理员签发设置链接")
        if credential.locked(self._now()):
            raise AuthenticationError("口令连续错误次数过多，请稍后再试")
        matched, _ = await self.hashing.verify(credential.password_hash, current)
        if not matched:
            await self._failure(credential)
            raise AuthenticationError("当前口令不正确")
        validate_new_password(new, username=credential.username)
        password_hash = await self.hashing.hash(new)
        user_id = principal.user_id
        await self._credentials.set_password(tenant_id, user_id, password_hash, at=self._now())
        await self._store.delete_user_login_sessions(tenant_id, user_id)
        await self._audit.control(principal, tenant_id, "password_changed", user_id=user_id)
        user = await self._active_user(tenant_id, user_id)
        if user is None:
            raise AuthenticationError("凭据无效")
        return await self._sign_in(user)

    async def username_of(self, tenant_id: str, user_id: str) -> str | None:
        """员工开通的用户名；没开通返回 None。"""
        credential = await self._credentials.get_credential(tenant_id, user_id)
        return credential.username if credential is not None else None

    # --- 共用 ---

    async def _find(self, tenant: str | None, username: str) -> LocalCredential | None:
        """按租户与用户名找凭据；租户不存在、停用或标识不合法，用户名不合规则，都当作找不到。"""
        tenant_id = tenant or self._settings.default_tenant
        if tenant_id is None:
            return None
        try:
            validate_tenant_id(tenant_id)
            name = normalize_username(username)
        except ValueError:
            return None
        found = await self._control_plane.get_tenant(tenant_id)
        if found is None or found.status != "active":
            return None
        return await self._credentials.find_credential(tenant_id, name)

    async def _active_user(self, tenant_id: str, user_id: str) -> UserRecord | None:
        """员工与租户都启用时返回员工，否则 None。"""
        user = await self._store.get_user(tenant_id, user_id)
        if user is None or user.status != "active":
            return None
        tenant = await self._control_plane.get_tenant(tenant_id)
        if tenant is None or tenant.status != "active":
            return None
        return user

    async def _failure(self, credential: LocalCredential) -> None:
        """口令错：计入连续失败（到阈值锁定），记审计与指标。"""
        updated = await self._credentials.record_login_failure(
            credential.tenant_id, credential.user_id, now=self._now(),
            lock_after=LOCK_AFTER_FAILURES, lock_seconds=LOCK_SECONDS,
        )  # fmt: skip
        locked = updated is not None and updated.locked(self._now())
        await self._rejected(credential, "locked_now" if locked else "bad_password")

    async def _rejected(self, credential: LocalCredential, reason: str) -> None:
        """认出了员工的失败：记 ``login_failed`` 与指标（对外的提示不区分原因）。"""
        # 只有锁定期内被拒的才计 locked；触发锁定的那次本身是口令错
        self._count("locked" if reason == "locked" else "rejected")
        await self._audit.login(
            credential.tenant_id,
            "login_failed",
            {"provider": PASSWORD, "reason": reason},
            user_id=credential.user_id,
        )

    async def _sign_in(self, user: UserRecord) -> SignedIn:
        """刷新登录时间，建登录会话（每次新令牌），记 ``login_succeeded``。"""
        now, ttl = self._now(), self._settings.session_ttl_seconds
        await self._store.update_user_profile(
            user.tenant_id, user.user_id, display_name=user.display_name,
            avatar_url=user.avatar_url, last_login_at=now,
        )  # fmt: skip
        token, token_hash = new_login_token()
        await self._store.create_login_session(
            LoginSession(
                token_hash=token_hash, tenant_id=user.tenant_id, user_id=user.user_id,
                created_at=now, expires_at=now + ttl, last_seen_at=now,
            )
        )  # fmt: skip
        self._count("succeeded")
        await self._audit.login(
            user.tenant_id, "login_succeeded", {"provider": PASSWORD}, user_id=user.user_id
        )
        return SignedIn(token, ttl, user.tenant_id, user.user_id)

    async def _revoke(self, previous: str | None) -> None:
        """登录成功时作废浏览器带着的旧登录会话（理由同飞书登录）。"""
        if previous is None:
            return
        token_hash = login_token_hash(previous)
        session = await self._store.get_login_session(token_hash, now=self._now())
        if session is not None:
            await self._store.delete_login_session(session.tenant_id, token_hash)

    def _require_public_url(self) -> str:
        """平台的对外地址：登录 cookie 与设置链接都依赖它。"""
        if self._settings.public_url is None:
            raise InvalidRequestError(
                "平台没有配置 public_url：不能签发登录 cookie 与设置链接，先在服务配置里配置它"
            )
        return self._settings.public_url

    def _count(self, result: PasswordLoginResult) -> None:
        """记一次口令登录结果的指标。"""
        if self._metrics is not None:
            self._metrics.password_login(result)


def _username(value: str) -> str:
    """规整管理员给的用户名，不合规则报 400。"""
    try:
        return normalize_username(value)
    except ValueError as exc:
        raise InvalidRequestError(str(exc)) from exc


__all__ = [
    "LOCK_AFTER_FAILURES",
    "LOCK_SECONDS",
    "PASSWORD",
    "RESET_LINK_TTL_SECONDS",
    "SETUP_FRAGMENT",
    "SETUP_LINK_TTL_SECONDS",
    "LocalAccounts",
    "SetupLink",
    "SetupLinkInfo",
    "SignedIn",
]
