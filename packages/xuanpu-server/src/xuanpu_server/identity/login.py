"""飞书登录的流程：开始（跳去飞书授权页）与完成（飞书回调）——设计 7.1，另有加固。

::

    GET /v1/auth/feishu/login?tenant=&return_to=
      租户存在且启用、配置了飞书、平台配了 public_url
      → 生成 state 与 PKCE 校验串，写登录尝试（10 分钟有效）
      → 另写登录尝试 cookie（独立随机数，登录尝试里只存摘要）
      → 302 到飞书授权页
    GET /v1/auth/feishu/callback?code=&state=（员工拒绝时是 error=access_denied）
      1. 按 state 取出并删除登录尝试；核对登录尝试 cookie；租户一律取自登录尝试
      2. 用授权码与校验串换令牌，3. 取用户信息
      4. 核对飞书企业：首次登录绑定，以后必须一致
      5. 按 (租户, feishu, union_id) 找用户，找不到建普通成员
      6. 用户已停用 → 拒绝
      7. 刷新姓名、头像、open_id 与最近登录时间
      8. 建登录会话（每次新令牌），写登录 cookie；浏览器带着的旧登录会话作废
      9. 302 到 return_to

失败一律 302 到落地页 ``identity.login_landing_path``，带 ``?login_error=<原因>``（七种原因见
``models.LoginFailureReason`` 与 http-api.md），计指标、记日志。授权码、飞书令牌、App Secret 不进
日志与审计；飞书令牌用完即丢，不保存。

**对设计的加固**（取舍记在实现计划的对齐表里）：

- 登录尝试 cookie（``identity.cookies``）防登录伪造；缺失、不符、同名不止一个都按 ``state_invalid``，
  且不拿授权码去换令牌。
- 匿名请求刷不出审计：只有飞书认出了一个人之后的失败（``AUDITED_REASONS``）才记 ``login_failed``，
  别的只计指标、记日志（与设计 7.3 事件回调「不写审计防被刷」同一口径）。
- 未认证的请求驱动不了平台反复调飞书：``state`` 形状不对不查库，``code`` 形状不对不调飞书，每个
  租户同时在调飞书的登录有上限（``identity.limits``）。
- 飞书的错误只有 20010 归为 ``not_in_app_scope``，别的（含授权码无效、App Secret 不对）都是
  ``identity_unavailable``。
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, NotFoundError, SecretMissingError
from xuanpu_server.identity.cookies import login_token_hash, new_login_token
from xuanpu_server.identity.feishu import FeishuError, code_challenge, new_code_verifier
from xuanpu_server.identity.limits import (
    FEISHU_CALLS_PER_TENANT,
    FEISHU_QUEUE_SECONDS,
    CallLimitBusyError,
    TenantCallLimit,
)
from xuanpu_server.identity.models import (
    ExternalIdentity,
    LoginAttempt,
    LoginSession,
    UserRecord,
    new_user_id,
)
from xuanpu_server.identity.urls import callback_url, safe_return_to, with_login_error
from xuanpu_server.tenancy import validate_tenant_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.identity.feishu import FeishuClient, FeishuUserInfo
    from xuanpu_server.identity.models import (
        IdentityProvider,
        LoginFailureReason,
        TenantIdentityConfig,
    )
    from xuanpu_server.identity.service import IdentitySettings
    from xuanpu_server.identity.store import IdentityStore
    from xuanpu_server.metrics import LoginResult, PlatformMetrics
    from xuanpu_server.secrets import SecretVault

logger = logging.getLogger(__name__)

FEISHU: IdentityProvider = "feishu"
LOGIN_ATTEMPT_TTL_SECONDS = 600
"""登录尝试（与登录尝试 cookie）的有效期：从开始登录算起 10 分钟。"""
MAX_CODE_LENGTH = 512
"""授权码的最大长度。"""
AUDITED_REASONS: frozenset[str] = frozenset(
    {"not_in_app_scope", "tenant_mismatch", "user_disabled"}
)
"""记 ``login_failed`` 审计的失败原因：飞书确实认出了一个人之后才会出现的这几种。"""
# state 是 secrets.token_urlsafe(32)：32 字节的 base64url，不带填充，恰好 43 个字符
_STATE = re.compile(r"^[A-Za-z0-9_-]{43}$")
# 授权码只收可见 ASCII；回调里 error 参数只有形如 access_denied 的才写进日志
_CODE = re.compile(r"^[\x21-\x7e]+$")
_ERROR_PARAM = re.compile(r"^[a-z_]{1,64}$")


class LoginFailedError(Exception):
    """登录失败（流程内部用：在最外层统一转成跳回落地页）。

    Attributes:
        reason: 失败原因。
        tenant_id: 知道是哪个、且确实存在的租户。
        user_id: 已经认出的员工（``user_disabled`` 时）。
        detail: 只写进服务端日志的说明，不含机密。
        warn: 记成告警（飞书出错、配置缺失、排队超时这类要运维看的）；否则记成普通信息。
    """

    def __init__(
        self,
        reason: LoginFailureReason,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        detail: str = "",
        warn: bool = False,
    ) -> None:
        """记下原因与上下文。"""
        super().__init__(f"{reason}：{detail}" if detail else reason)
        self.reason: LoginFailureReason = reason
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.detail = detail
        self.warn = warn


@dataclass(frozen=True)
class LoginRedirect:
    """登录流程一步的结果：接入层据此回 302 并写 cookie。

    Attributes:
        location: 跳去哪：飞书授权页、``return_to`` 或带 ``login_error`` 的落地页。
        attempt_cookie: 要写进浏览器的登录尝试 cookie 的值（开始登录成功时才有）。
        session_token: 要写进登录 cookie 的令牌（登录成功时才有）。
        session_max_age: 登录 cookie 的有效期（秒），与登录会话一致。
    """

    location: str
    attempt_cookie: str | None = field(default=None, repr=False)
    session_token: str | None = field(default=None, repr=False)
    session_max_age: int = 0


class FeishuLogin:
    """飞书登录的流程。由 ``IdentityService`` 组合，接入层经 ``IdentityService.login`` 调用。

    Attributes:
        feishu: 飞书接口的客户端（测试时换成接模拟飞书的）。
        feishu_limit: 每个租户同时在调飞书的登录上限（排队超时按 ``identity_unavailable``）。
    """

    def __init__(
        self,
        *,
        store: IdentityStore,
        control_plane: ControlPlaneStore,
        vault: SecretVault,
        audit: AuditLog,
        settings: IdentitySettings,
        feishu: FeishuClient,
        metrics: PlatformMetrics | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            store: 身份存储。
            control_plane: 控制面存储（查租户是否存在、是否启用）。
            vault: 密钥保管库（取 App Secret）。
            audit: 审计写入口。
            settings: 身份域的平台级设置。
            feishu: 飞书接口的客户端。
            metrics: 平台指标；None 不打点。
            now: 取当前时间的函数，测试时可替换。
        """
        self.feishu = feishu
        self._store = store
        self._control_plane = control_plane
        self._vault = vault
        self._audit = audit
        self._settings = settings
        self._metrics = metrics
        self._now = now
        self.feishu_limit = TenantCallLimit(
            limit=FEISHU_CALLS_PER_TENANT, queue_seconds=FEISHU_QUEUE_SECONDS
        )

    # --- 开始 ---

    async def begin(self, *, tenant: str | None, return_to: str | None) -> LoginRedirect:
        """开始登录：跳去飞书授权页，或（失败时）跳回落地页。"""
        try:
            return await self._begin(tenant, return_to)
        except LoginFailedError as failure:
            return await self._failed(failure)

    async def _begin(self, tenant: str | None, return_to: str | None) -> LoginRedirect:
        """开始登录的各步，失败抛 ``LoginFailedError``。"""
        tenant_id = await self._active_tenant(tenant or self._settings.default_tenant)
        public_url = self._public_url(tenant_id)
        config = await self._config(tenant_id)
        now = self._now()
        state = secrets.token_urlsafe(32)
        verifier = new_code_verifier()
        # 登录尝试 cookie 的值：与 state 无关的另一个随机数，库里只存它的摘要
        cookie = secrets.token_urlsafe(32)
        await self._store.create_login_attempt(
            LoginAttempt(
                state=state,
                tenant_id=tenant_id,
                code_verifier=verifier,
                return_to=safe_return_to(return_to, fallback=self._settings.login_landing_path),
                created_at=now,
                expires_at=now + LOGIN_ATTEMPT_TTL_SECONDS,
                cookie_hash=login_token_hash(cookie),
            )
        )
        location = self.feishu.authorize_url(
            app_id=config.app_id,
            redirect_uri=callback_url(public_url),
            state=state,
            code_challenge=code_challenge(verifier),
        )
        return LoginRedirect(location, attempt_cookie=cookie)

    # --- 完成 ---

    async def complete(
        self,
        *,
        state: str | None,
        code: str | None,
        error: str | None,
        attempt_cookie: str | None,
        session_cookie: str | None = None,
    ) -> LoginRedirect:
        """完成登录（飞书回调）：签发登录会话并跳去 ``return_to``，或跳回落地页。

        Args:
            state: 回调带回的 ``state``。
            code: 授权码；员工拒绝时没有。
            error: 飞书给的错误（员工拒绝时是 ``access_denied``）。
            attempt_cookie: 浏览器带来的登录尝试 cookie；没带或同名不止一个时为 None。
            session_cookie: 浏览器带来的（旧的）登录 cookie；登录成功后它对应的登录会话作废。
        """
        try:
            redirect = await self._complete(state, code, error, attempt_cookie)
        except LoginFailedError as failure:
            return await self._failed(failure)
        await self._revoke(session_cookie)
        return redirect

    async def _complete(
        self, state: str | None, code: str | None, error: str | None, attempt_cookie: str | None
    ) -> LoginRedirect:
        """完成登录的各步（模块说明的 1–8），失败抛 ``LoginFailedError``。"""
        attempt = await self._take_attempt(state, attempt_cookie)
        tenant_id = attempt.tenant_id  # 租户一律取自登录尝试，不接受请求参数
        if error is not None:
            # 员工拒绝时飞书只带 error；error 与 code 同时出现也按 error 处理
            shown = error if _ERROR_PARAM.match(error) else "（形状不对，不记）"
            reason: LoginFailureReason = (
                "feishu_denied" if error == "access_denied" else "identity_unavailable"
            )
            raise LoginFailedError(reason, tenant_id=tenant_id, detail=f"飞书回调带 error={shown}")
        if not code or len(code) > MAX_CODE_LENGTH or not _CODE.match(code):
            raise LoginFailedError(
                "identity_unavailable", tenant_id=tenant_id, detail="回调没有 code，或形状不对"
            )
        await self._active_tenant(tenant_id)
        public_url = self._public_url(tenant_id)
        config = await self._config(tenant_id)
        info = await self._feishu_user(config, attempt, code, public_url)
        await self._check_tenant_key(config, info)
        user = await self._user_for(tenant_id, info)
        if user.status != "active":
            raise LoginFailedError("user_disabled", tenant_id=tenant_id, user_id=user.user_id)
        return await self._sign_in(user, info, config, attempt.return_to)

    async def _take_attempt(self, state: str | None, attempt_cookie: str | None) -> LoginAttempt:
        """取出并删除登录尝试，核对登录尝试 cookie。形状不对的 state 不查库。"""
        if state is None or not _STATE.match(state):
            raise LoginFailedError("state_invalid", detail="回调没有 state，或形状不对")
        attempt = await self._store.take_login_attempt(state, now=self._now())
        if attempt is None:
            raise LoginFailedError("state_invalid", detail="登录尝试不存在、已用过或已过期")
        presented = login_token_hash(attempt_cookie) if attempt_cookie is not None else ""
        if not hmac.compare_digest(presented, attempt.cookie_hash):
            raise LoginFailedError(
                "state_invalid", tenant_id=attempt.tenant_id, detail="登录尝试 cookie 缺失或不符"
            )
        return attempt

    async def _feishu_user(
        self, config: TenantIdentityConfig, attempt: LoginAttempt, code: str, public_url: str
    ) -> FeishuUserInfo:
        """用授权码换令牌，再取用户信息（占本租户的一个调飞书名额）。令牌用完即丢。"""
        tenant_id = config.tenant_id
        try:
            app_secret = await self._vault.get(tenant_id, config.app_secret_ref)
        except SecretMissingError as exc:
            raise LoginFailedError(
                "tenant_unavailable",
                tenant_id=tenant_id,
                detail="飞书 App Secret 不在保管库里",
                warn=True,
            ) from exc
        try:
            async with self.feishu_limit.slot(tenant_id):
                token = await self.feishu.exchange_code(
                    app_id=config.app_id,
                    app_secret=app_secret,
                    code=code,
                    redirect_uri=callback_url(public_url),
                    code_verifier=attempt.code_verifier,
                )
                return await self.feishu.user_info(token)
        except CallLimitBusyError as exc:
            raise LoginFailedError(
                "identity_unavailable", tenant_id=tenant_id, detail=str(exc), warn=True
            ) from exc
        except FeishuError as exc:
            scope = exc.kind == "not_in_app_scope"
            reason: LoginFailureReason = "not_in_app_scope" if scope else "identity_unavailable"
            raise LoginFailedError(
                reason, tenant_id=tenant_id, detail=str(exc), warn=not scope
            ) from exc

    async def _check_tenant_key(self, config: TenantIdentityConfig, info: FeishuUserInfo) -> None:
        """核对飞书企业：首次成功登录绑定，以后必须一致。"""
        try:
            bound = await self._store.bind_tenant_key(config.tenant_id, FEISHU, info.tenant_key)
        except NotFoundError as exc:
            # 飞书配置在这期间被删了
            raise LoginFailedError(
                "tenant_unavailable", tenant_id=config.tenant_id, detail="租户的飞书配置不见了"
            ) from exc
        if bound != info.tenant_key:
            raise LoginFailedError(
                "tenant_mismatch",
                tenant_id=config.tenant_id,
                detail=f"飞书企业 {info.tenant_key!r} 与租户绑定的 {bound!r} 不一致",
            )

    async def _user_for(self, tenant_id: str, info: FeishuUserInfo) -> UserRecord:
        """按 ``union_id`` 找用户；找不到就建普通成员，记 ``user_created``。

        用户与外部身份在一个原子操作里一起建：同一人并发首登时只建出一个用户。
        """
        existing = await self._store.get_user_by_identity(tenant_id, FEISHU, info.union_id)
        if existing is not None:
            return existing
        user = UserRecord(
            tenant_id=tenant_id,
            user_id=new_user_id(),
            display_name=info.name,
            created_at=self._now(),
            avatar_url=info.avatar_url,
        )
        identity = ExternalIdentity(
            tenant_id=tenant_id,
            provider=FEISHU,
            subject=info.union_id,
            user_id=user.user_id,
            attributes={"open_id": info.open_id},
        )
        try:
            await self._store.create_user(user, identity=identity)
        except ConflictError:
            # 同一人并发首登（开了两个标签页）：后到的这次因外部身份冲突失败，重查先建的那个
            raced = await self._store.get_user_by_identity(tenant_id, FEISHU, info.union_id)
            if raced is None:
                raise
            return raced
        data = {"provider": FEISHU, "display_name": user.display_name, "role": user.role}
        await self._audit.login(tenant_id, "user_created", data, user_id=user.user_id)
        return user

    async def _sign_in(
        self, user: UserRecord, info: FeishuUserInfo, config: TenantIdentityConfig, return_to: str
    ) -> LoginRedirect:
        """刷新员工信息，建登录会话（每次新令牌），记 ``login_succeeded``。"""
        tenant_id, now = user.tenant_id, self._now()
        await self._store.update_user_profile(
            tenant_id,
            user.user_id,
            display_name=info.name,
            avatar_url=info.avatar_url,
            last_login_at=now,
        )
        await self._refresh_open_id(user, info)
        ttl = config.session_ttl_seconds or self._settings.session_ttl_seconds
        token, token_hash = new_login_token()
        await self._store.create_login_session(
            LoginSession(
                token_hash=token_hash,
                tenant_id=tenant_id,
                user_id=user.user_id,
                created_at=now,
                expires_at=now + ttl,
                last_seen_at=now,
            )
        )
        self._count("succeeded")
        await self._audit.login(
            tenant_id, "login_succeeded", {"provider": FEISHU}, user_id=user.user_id
        )
        return LoginRedirect(return_to, session_token=token, session_max_age=ttl)

    async def _refresh_open_id(self, user: UserRecord, info: FeishuUserInfo) -> None:
        """外部身份的 ``open_id`` 变了才写（应用重建后会变）；属性里别的项保留。"""
        identities = await self._store.list_external_identities(
            user.tenant_id, FEISHU, user_id=user.user_id, limit=1
        )
        current = identities[0].attributes if identities else {}
        if current.get("open_id") != info.open_id:
            await self._store.update_identity_attributes(
                user.tenant_id, FEISHU, info.union_id, {**current, "open_id": info.open_id}
            )

    async def _revoke(self, previous: str | None) -> None:
        """登录成功时，浏览器带着的旧登录 cookie 对应的登录会话作废。

        浏览器里的旧 cookie 已被新令牌覆盖，不作废它，那个会话只能等过期；拿到过旧令牌的人
        也就还能接着用。只认确实存在的登录会话，租户取自它的记录。
        """
        if previous is None:
            return
        token_hash = login_token_hash(previous)
        session = await self._store.get_login_session(token_hash, now=self._now())
        if session is not None:
            await self._store.delete_login_session(session.tenant_id, token_hash)

    # --- 共用 ---

    async def _active_tenant(self, tenant_id: str | None) -> str:
        """租户必须存在且启用。"""
        if tenant_id is None:
            raise LoginFailedError(
                "tenant_unavailable", detail="没有指定租户，也没有配置 identity.default_tenant"
            )
        try:
            validate_tenant_id(tenant_id)
        except ValueError as exc:
            raise LoginFailedError("tenant_unavailable", detail="租户标识不合法") from exc
        tenant = await self._control_plane.get_tenant(tenant_id)
        if tenant is None:
            raise LoginFailedError("tenant_unavailable", detail=f"租户 {tenant_id} 不存在")
        if tenant.status != "active":
            raise LoginFailedError("tenant_unavailable", tenant_id=tenant_id, detail="租户已停用")
        return tenant_id

    def _public_url(self, tenant_id: str) -> str:
        """平台的对外地址；没配就不能用飞书登录。"""
        if self._settings.public_url is None:
            raise LoginFailedError(
                "tenant_unavailable", tenant_id=tenant_id, detail="平台没有配置 public_url"
            )
        return self._settings.public_url

    async def _config(self, tenant_id: str) -> TenantIdentityConfig:
        """租户的飞书配置；没配置就不能用飞书登录。"""
        config = await self._store.get_identity_config(tenant_id, FEISHU)
        if config is None:
            raise LoginFailedError(
                "tenant_unavailable", tenant_id=tenant_id, detail="租户没有配置飞书"
            )
        return config

    async def _failed(self, failure: LoginFailedError) -> LoginRedirect:
        """失败：计数、记日志；飞书认出了人的那几种另记 ``login_failed``；跳回落地页带上原因。"""
        self._count(failure.reason)
        level = logging.WARNING if failure.warn else logging.INFO
        logger.log(level, "飞书登录失败（租户 %s）：%s", failure.tenant_id, failure)
        if failure.tenant_id is not None and failure.reason in AUDITED_REASONS:
            await self._audit.login(
                failure.tenant_id,
                "login_failed",
                {"reason": failure.reason},
                user_id=failure.user_id,
            )
        return LoginRedirect(with_login_error(self._settings.login_landing_path, failure.reason))

    def _count(self, result: LoginResult) -> None:
        """记一次登录结果的指标。"""
        if self._metrics is not None:
            self._metrics.login(result)


__all__ = [
    "AUDITED_REASONS",
    "LOGIN_ATTEMPT_TTL_SECONDS",
    "MAX_CODE_LENGTH",
    "FeishuLogin",
    "LoginFailedError",
    "LoginRedirect",
]
