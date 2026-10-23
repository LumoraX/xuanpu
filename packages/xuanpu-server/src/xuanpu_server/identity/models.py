"""身份域的记录类型：平台用户、外部身份、租户身份配置、登录会话、登录尝试、按用户用量、对账状态。

全部不可变；改动状态时生成新记录写回存储（与控制面记录同一做法）。取值受限的字段在构造时
校验，从库里读回的脏数据当场报错，不带着错的值往下走。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, get_args

from xuanpu_server.ids import new_id
from xuanpu_server.tenancy import TenantRole

UserStatus = Literal["active", "disabled"]
DisabledReason = Literal["feishu_resigned", "feishu_frozen", "feishu_exited", "admin"]
IdentityProvider = Literal["feishu", "wecom"]
LoginFailureReason = Literal[
    "feishu_denied",
    "not_in_app_scope",
    "state_invalid",
    "tenant_mismatch",
    "tenant_unavailable",
    "user_disabled",
    "identity_unavailable",
]
"""飞书登录失败的原因（设计 7.1），也是落地页 ``?login_error=`` 的取值。"""

USER_ID_PREFIX = "usr"
MAX_PAGE_SIZE = 1000


def new_user_id() -> str:
    """分配一个平台用户标识：``usr_`` 加 24 位十六进制（``ids.new_id`` 的统一格式）。"""
    return new_id(USER_ID_PREFIX)


def require_choice(name: str, value: object, allowed: tuple[str, ...]) -> None:
    """取值必须是 ``allowed`` 之一，否则报 ``ValueError``。"""
    if value not in allowed:
        raise ValueError(f"{name} 必须是 {' / '.join(allowed)} 之一，得到 {value!r}")


def require_page_size(limit: int) -> None:
    """单页条数必须在 1 到 ``MAX_PAGE_SIZE`` 之间。"""
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit 必须在 1 到 {MAX_PAGE_SIZE} 之间，得到 {limit}")


def require_status_change(status: UserStatus, reason: DisabledReason | None) -> None:
    """改状态的参数要自洽：停用必须给原因，启用不能给原因。"""
    require_choice("status", status, get_args(UserStatus))
    if status == "disabled":
        if reason is None:
            raise ValueError("停用必须给出 reason")
        require_choice("reason", reason, get_args(DisabledReason))
    elif reason is not None:
        raise ValueError("启用时不能给 reason")


def require_reconcile_window(interval: float, lease_seconds: float) -> None:
    """对账认领的参数：周期不能为负（手工触发传 0），租约时长必须为正。"""
    if interval < 0:
        raise ValueError(f"interval 不能为负，得到 {interval}")
    if lease_seconds <= 0:
        raise ValueError(f"lease_seconds 必须为正数，得到 {lease_seconds}")


@dataclass(frozen=True)
class UserRecord:
    """一名平台用户（员工）。

    Attributes:
        tenant_id: 所属租户。
        user_id: 平台分配的标识（``new_user_id``）。平台内部各处一律用它，飞书的标识只作外部映射。
        display_name: 显示名；每次登录按身份源返回的刷新。
        role: ``tenant_admin`` 或 ``tenant_member``（``tenancy.TenantRole``）。
        created_at: 建账号的时间。
        avatar_url: 头像地址；每次登录刷新。
        status: ``active`` 或 ``disabled``。
        disabled_reason: 停用原因：``feishu_resigned``、``feishu_frozen``、``feishu_exited``、
            ``admin``。停用时必有，启用时为空。
        last_login_at: 最近一次登录的时间；从未登录过为空。
        disabled_at: 停用的时间。停用时必有，启用时为空。
        unverified_since: 对账时在身份源查不到此人的起始时间；再次查到时清空。
    """

    tenant_id: str
    user_id: str
    display_name: str
    created_at: float
    role: TenantRole = "tenant_member"
    avatar_url: str | None = None
    status: UserStatus = "active"
    disabled_reason: DisabledReason | None = None
    last_login_at: float | None = None
    disabled_at: float | None = None
    unverified_since: float | None = None
    lifecycle_epoch: int = 1
    profile_version: int = 1

    def __post_init__(self) -> None:
        """角色、状态取值受限；停用原因与时间和状态一致。"""
        require_choice("role", self.role, get_args(TenantRole))
        if self.lifecycle_epoch < 1 or self.profile_version < 1:
            raise ValueError('身份权限版本必须为正整数')
        require_choice("status", self.status, get_args(UserStatus))
        if self.status == "active":
            if self.disabled_reason is not None or self.disabled_at is not None:
                raise ValueError("启用状态的用户不应带停用原因与停用时间")
            return
        if self.disabled_reason is None or self.disabled_at is None:
            raise ValueError("停用状态的用户必须带停用原因与停用时间")
        require_choice("disabled_reason", self.disabled_reason, get_args(DisabledReason))


@dataclass(frozen=True)
class ExternalIdentity:
    """用户在某个身份源里的标识。``(tenant_id, provider, subject)`` 唯一。

    Attributes:
        tenant_id: 所属租户。
        provider: 身份源，员工映射支持 ``feishu`` 和 ``wecom``。
        subject: 身份源里的稳定标识；飞书用 ``union_id``。
        user_id: 对应的平台用户。
        attributes: 身份源的其他标识（飞书存 ``open_id``，供以后调飞书接口用）。
    """

    tenant_id: str
    provider: IdentityProvider
    subject: str
    user_id: str
    attributes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """身份源取值受限。"""
        require_choice("provider", self.provider, get_args(IdentityProvider))


def require_identity_of(user: UserRecord, identity: ExternalIdentity) -> None:
    """与用户一起建的外部身份必须属于同一租户、同一用户，否则报 ``ValueError``。"""
    if identity.tenant_id != user.tenant_id:
        raise ValueError("外部身份的 tenant_id 与用户不一致")
    if identity.user_id != user.user_id:
        raise ValueError("外部身份的 user_id 与用户不一致")


@dataclass(frozen=True)
class TenantIdentityConfig:
    """租户接入某个身份源的配置。三个密钥只存保管库里的名字，明文不在这里。

    Attributes:
        tenant_id: 租户。
        provider: 身份源；第一版只有 ``feishu``。
        app_id: 飞书自建应用的 App ID。
        app_secret_ref: App Secret 在保管库里的名字。
        encrypt_key_ref: Encrypt Key 在保管库里的名字；未配置时事件关闭。
        verification_token_ref: Verification Token 在保管库里的名字；可单独保留，但两项齐备才启用事件。
        feishu_tenant_key: 首次成功登录时绑定的飞书企业标识；之后的登录与事件都必须一致。
            只经 ``IdentityStore.bind_tenant_key`` 写入，``put_identity_config`` 不改它。
        session_ttl_seconds: 登录会话有效期；为空时用平台默认值。
    """

    tenant_id: str
    provider: Literal["feishu"]
    app_id: str
    app_secret_ref: str
    encrypt_key_ref: str | None = None
    verification_token_ref: str | None = None
    feishu_tenant_key: str | None = None
    session_ttl_seconds: int | None = None

    def __post_init__(self) -> None:
        """身份源取值受限；有效期为正数。"""
        require_choice("provider", self.provider, ("feishu",))
        for name in ("encrypt_key_ref", "verification_token_ref"):
            if getattr(self, name) == "":
                object.__setattr__(self, name, None)
        if self.session_ttl_seconds is not None and self.session_ttl_seconds < 1:
            raise ValueError(
                f"session_ttl_seconds 必须为正数或 None，得到 {self.session_ttl_seconds}"
            )

    @property
    def events_enabled(self) -> bool:
        """事件仅在加密密钥与验证令牌引用都配置时启用，登录不依赖这两项。"""
        return bool(self.encrypt_key_ref and self.verification_token_ref)


@dataclass(frozen=True)
class LoginSession:
    """一个浏览器登录会话。

    Attributes:
        token_hash: 登录令牌的 SHA-256；令牌本身不落库。
        tenant_id: 所属租户。
        user_id: 登录的用户。
        created_at: 登录时间。
        expires_at: 到期时刻；从登录时刻算起，不滑动。
        last_seen_at: 最近使用的时间（节流由服务层做，存储只管写）。
    """

    token_hash: str
    tenant_id: str
    user_id: str
    created_at: float
    expires_at: float
    last_seen_at: float


@dataclass(frozen=True)
class LoginAttempt:
    """一次进行中的登录：从跳去身份源授权页到回调回来。取出即删，只能用一次。

    Attributes:
        state: 随机数；授权回调按它找回这次登录。
        tenant_id: 要登录的租户。
        code_verifier: PKCE 校验串。
        return_to: 登录完成后的站内落地路径。
        created_at: 发起时间。
        expires_at: 到期时刻；有效期由服务层在建记录时算好。
        cookie_hash: 登录尝试 cookie 里那个随机数的 SHA-256。回调时浏览器出示的 cookie 必须与它
            对上，防登录伪造：别人把自己那次登录的回调链接发给员工，员工的浏览器没有对应的 cookie。
    """

    state: str
    tenant_id: str
    code_verifier: str = field(repr=False)
    return_to: str
    created_at: float
    expires_at: float
    cookie_hash: str = field(repr=False)


@dataclass(frozen=True)
class UserUsageRecord:
    """某租户某一天某名员工的用量。计数字段与 ``UsageRecord`` 相同。"""

    tenant_id: str
    day: str
    user_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    turns: int = 0


@dataclass(frozen=True)
class ReconcileState:
    """某租户的对账状态：保证多个实例每个周期只对一次。

    Attributes:
        tenant_id: 租户。
        last_run_at: 上次完成的时间；从未完成为空。
        lease_owner: 正在对账的持有者；无人持有为空。
        lease_expires_at: 持有的到期时刻。
    """

    tenant_id: str
    last_run_at: float | None = None
    lease_owner: str | None = None
    lease_expires_at: float = 0.0

    def claimable(self, now: float, interval: float) -> bool:
        """在 ``now`` 这一刻能否认领：没有未到期的持有者，且距上次完成已满 ``interval`` 秒。

        持有者自己也不能在持有期间再认领一次，免得同一实例的定时对账与手工对账同时跑。
        与 Postgres 实现里认领的条件一致。
        """
        lease_free = self.lease_owner is None or self.lease_expires_at <= now
        due = self.last_run_at is None or self.last_run_at + interval <= now
        return lease_free and due


@dataclass(frozen=True)
class UserQuery:
    """列用户的条件。各条件是 AND 关系；结果按 ``user_id`` 升序。

    Attributes:
        status: 只看某种状态。
        role: 只看某种角色。
        unverified: True 只看对账时查不到的，False 只看查得到的，None 不限。
        after: 翻页位置：只看 ``user_id`` 大于它的。
        limit: 最多返回多少条。
    """

    status: UserStatus | None = None
    role: TenantRole | None = None
    unverified: bool | None = None
    after: str | None = None
    limit: int = 100

    def __post_init__(self) -> None:
        """限制单页大小。"""
        require_page_size(self.limit)

    def matches(self, user: UserRecord) -> bool:
        """某个用户是否命中（含翻页位置，不含条数）。"""
        checks = (
            self.status is None or user.status == self.status,
            self.role is None or user.role == self.role,
            self.unverified is None or (user.unverified_since is not None) == self.unverified,
            self.after is None or user.user_id > self.after,
        )
        return all(checks)


__all__ = [
    "MAX_PAGE_SIZE",
    "USER_ID_PREFIX",
    "DisabledReason",
    "ExternalIdentity",
    "IdentityProvider",
    "LoginAttempt",
    "LoginFailureReason",
    "LoginSession",
    "ReconcileState",
    "TenantIdentityConfig",
    "UserQuery",
    "UserRecord",
    "UserStatus",
    "UserUsageRecord",
    "new_user_id",
    "require_choice",
    "require_identity_of",
    "require_page_size",
    "require_reconcile_window",
    "require_status_change",
]
