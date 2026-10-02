"""租户、凭据与调用者身份。

租户只存在于平台层（红线 2）：内核与适配包都不知道租户，平台在组装引擎时把按租户隔离的
资源注入进去（红线 5）。平台里所有读写都要带 ``Principal``，由它给出租户边界。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Literal

from xuanpu_server.errors import AuthenticationError, PermissionDeniedError

TenantRole = Literal["tenant_admin", "tenant_member"]
"""租户内的角色：API key 与平台用户（员工）都只能是这两种之一。"""
Role = Literal["platform_admin", TenantRole]
"""调用者的角色：租户内的两种，加上只来自根凭据的平台管理员。"""
TenantStatus = Literal["active", "suspended"]

API_KEY_PREFIX = "xp"
ROOT_KEY_PREFIX = "xproot"
METRICS_TOKEN_PREFIX = "xpmetrics"

_TENANT_ID = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")
_KEY_ID = re.compile(r"^[0-9a-f]{12}$")
_SYSTEM_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


@dataclass(frozen=True)
class QuotaLimits:
    """租户配额。None 表示不限。

    Attributes:
        max_active_sessions: 同时处于活动状态的会话数上限。
        max_daily_tokens: 每个自然日（UTC）的 token 用量上限。
        max_concurrent_turns: 同一时刻进行中的对话数上限（本实例内计数）。
    """

    max_active_sessions: int | None = None
    max_daily_tokens: int | None = None
    max_concurrent_turns: int | None = None

    def __post_init__(self) -> None:
        """上限必须为正数。"""
        for name in ("max_active_sessions", "max_daily_tokens", "max_concurrent_turns"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} 必须为正数或 None，得到 {value}")


@dataclass(frozen=True)
class Tenant:
    """一个租户。"""

    tenant_id: str
    name: str
    status: TenantStatus = "active"
    quota: QuotaLimits = QuotaLimits()
    created_at: float = 0.0


@dataclass(frozen=True)
class ApiKeyRecord:
    """一把 API key 的存档。只存密钥的摘要，不存明文。

    Attributes:
        can_act_as_user: 能否代员工调用（请求带 ``X-Xuanpu-Act-As``）。要在签发时显式授予。
    """

    key_id: str
    tenant_id: str
    name: str
    role: Role
    secret_hash: str
    created_at: float
    revoked_at: float | None = None
    can_act_as_user: bool = False

    @property
    def active(self) -> bool:
        """是否仍然有效。"""
        return self.revoked_at is None


_ROLE_RANK: dict[str, int] = {"tenant_member": 0, "tenant_admin": 1, "platform_admin": 2}


def lower_role(first: Role, second: Role) -> Role:
    """两个角色中权限较低的那个：``tenant_member`` < ``tenant_admin`` < ``platform_admin``。

    代员工调用时，生效角色取 key 的角色与员工角色中较低的那个，防止普通成员的 key 借管理员
    身份越权（反过来，管理员的 key 代普通成员调用，也只有成员的权限）。
    """
    return first if _ROLE_RANK[first] <= _ROLE_RANK[second] else second


@dataclass(frozen=True)
class Principal:
    """一次调用的调用者。

    经接口来的调用者有四种，``key_id`` 与 ``user_id`` 至少有一个；另有平台自己在内部发起操作时用的
    系统调用者（``system_principal``），不经任何接口签发：

    | 调用者 | ``key_id`` | ``user_id`` | ``actor`` |
    | --- | --- | --- | --- |
    | 根凭据 | ``"root"`` | 空 | ``key:root`` |
    | API key | key 的标识 | 空 | ``key:<key_id>`` |
    | 浏览器登录（cookie） | 空 | 员工 | ``user:<user_id>`` |
    | 系统代员工（带 ``X-Xuanpu-Act-As``） | key 的标识 | 员工 | ``key:<key_id>/user:<user_id>`` |
    | 系统调用者（平台内部，``system`` 为名字） | 空 | 空 | ``system:<名字>`` |

    Attributes:
        role: 生效的角色。代员工调用时是 key 与员工两者中较低的那个（``lower_role``）。
        tenant_id: 所属租户；平台管理员不属于任何租户，为 None。
        key_id: 所用 API key 的标识；根凭据为 ``"root"``；浏览器登录没有 key，为 None。
        user_id: 代表的员工（平台用户标识）；不代表任何员工的 API key 与根凭据为 None。
        system: 系统调用者的名字（如 ``feishu-event``）；经接口来的调用者为 None。
    """

    role: Role
    tenant_id: str | None
    key_id: str | None
    user_id: str | None = None
    system: str | None = None

    def __post_init__(self) -> None:
        """调用者总要落到一把 key、一名员工或一个系统名字上；员工与系统调用者总属于某个租户。"""
        if self.system is not None:
            if self.tenant_id is None:
                raise ValueError("系统调用者必须属于某个租户")
            if self.key_id is not None or self.user_id is not None:
                raise ValueError("系统调用者不带 key 与员工")
            if not _SYSTEM_NAME.match(self.system):
                raise ValueError(
                    f"系统调用者的名字不合法：{self.system!r}（小写字母开头，只含小写字母、数字、连字符）"
                )
            return
        if self.key_id is None and self.user_id is None:
            raise ValueError("调用者必须有 key_id 或 user_id")
        if self.user_id is not None and self.tenant_id is None:
            raise ValueError("代表员工的调用者必须属于某个租户")

    @property
    def actor(self) -> str:
        """写进审计记录的调用者标识（取值见类说明里的表）。"""
        if self.system is not None:
            return f"system:{self.system}"
        if self.key_id is None:
            return f"user:{self.user_id}"
        if self.user_id is None:
            return f"key:{self.key_id}"
        return f"key:{self.key_id}/user:{self.user_id}"

    def require_tenant(self) -> str:
        """取调用者的租户；平台管理员没有租户上下文，不能做租户内的操作。"""
        if self.tenant_id is None:
            raise PermissionDeniedError("这个操作需要租户凭据，平台管理员凭据不能直接使用")
        return self.tenant_id

    def require_tenant_admin(self) -> str:
        """要求调用者是租户管理员，返回其租户。"""
        tenant_id = self.require_tenant()
        if self.role != "tenant_admin":
            raise PermissionDeniedError("这个操作需要租户管理员权限")
        return tenant_id

    def require_platform_admin(self) -> None:
        """要求调用者是平台管理员。"""
        if self.role != "platform_admin":
            raise PermissionDeniedError("这个操作需要平台管理员权限")


def system_principal(tenant_id: str, name: str) -> Principal:
    """平台在某个租户里自己发起操作时的调用者（飞书离职事件、对账、停用后的收尾）。

    角色是 ``tenant_admin``，只在这一个租户里；不代表任何员工，所以可见范围是本租户全部会话。审计里
    ``actor`` 是 ``system:<name>``。只在平台内部构造，不经任何接口签发，调用方拿不到它。
    """
    return Principal(role="tenant_admin", tenant_id=tenant_id, key_id=None, system=name)


def validate_tenant_id(tenant_id: str) -> str:
    """租户标识会出现在目录名、表名前缀里，字符集必须受限。"""
    if not _TENANT_ID.match(tenant_id):
        raise ValueError(
            f"租户标识非法：{tenant_id!r}（3–40 个字符，小写字母开头，只含小写字母、数字、连字符）"
        )
    return tenant_id


def tenant_directory(tenant_id: str) -> str:
    """租户在本机目录（会话存储、工作区、业务包缓存）下的子目录名。

    租户标识本身的字符集已受限；再拼上摘要，避免大小写不敏感的文件系统上两个租户撞目录。
    """
    digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:8]
    return f"{tenant_id}-{digest}"


def hash_secret(secret: str) -> str:
    """密钥的摘要。密钥本身是高熵随机串，用 SHA-256 足够，不需要慢哈希。"""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """生成一把 API key，返回 ``(完整的 key, key_id, 密钥摘要)``。

    完整的 key 只在创建时返回给调用方一次，平台不保存。
    """
    key_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    return f"{API_KEY_PREFIX}_{key_id}_{secret}", key_id, hash_secret(secret)


def generate_root_key() -> tuple[str, str]:
    """生成平台根凭据，返回 ``(完整的 key, 摘要)``。摘要写进服务配置。"""
    secret = secrets.token_urlsafe(40)
    return f"{ROOT_KEY_PREFIX}_{secret}", hash_secret(secret)


def generate_metrics_token() -> tuple[str, str]:
    """生成指标抓取令牌，返回 ``(完整的令牌, 摘要)``。摘要写进服务配置的 ``metrics.token_hash``。

    与根凭据同一种做法：平台只存摘要；前缀不同，两者不能互相顶替。抓取令牌只能读 ``/metrics``，
    不是调用者身份。
    """
    secret = secrets.token_urlsafe(32)
    return f"{METRICS_TOKEN_PREFIX}_{secret}", hash_secret(secret)


def parse_api_key(presented: str) -> tuple[str, str]:
    """拆出 ``(key_id, 密钥)``。

    Raises:
        AuthenticationError: 格式不对。
    """
    prefix, _, rest = presented.partition("_")
    key_id, _, secret = rest.partition("_")
    if prefix != API_KEY_PREFIX or not _KEY_ID.match(key_id) or not secret:
        raise AuthenticationError("凭据无效")
    return key_id, secret


def verify_secret(secret: str, expected_hash: str) -> bool:
    """常数时间比较，避免按响应时间猜出摘要。"""
    return hmac.compare_digest(hash_secret(secret), expected_hash)


def _verify_prefixed(presented: str, prefix: str, expected_hash: str) -> bool:
    """校验 ``<前缀>_<密钥>`` 形状的凭据：前缀要对，密钥的摘要做常数时间比较。"""
    actual, _, secret = presented.partition("_")
    if actual != prefix or not secret:
        return False
    return verify_secret(secret, expected_hash)


def verify_root_key(presented: str, root_key_hash: str) -> bool:
    """校验平台根凭据。"""
    return _verify_prefixed(presented, ROOT_KEY_PREFIX, root_key_hash)


def verify_metrics_token(presented: str, token_hash: str) -> bool:
    """校验指标抓取令牌。"""
    return _verify_prefixed(presented, METRICS_TOKEN_PREFIX, token_hash)


__all__ = [
    "API_KEY_PREFIX",
    "METRICS_TOKEN_PREFIX",
    "ROOT_KEY_PREFIX",
    "ApiKeyRecord",
    "Principal",
    "QuotaLimits",
    "Role",
    "Tenant",
    "TenantRole",
    "TenantStatus",
    "generate_api_key",
    "generate_metrics_token",
    "generate_root_key",
    "hash_secret",
    "lower_role",
    "parse_api_key",
    "system_principal",
    "tenant_directory",
    "validate_tenant_id",
    "verify_metrics_token",
    "verify_root_key",
    "verify_secret",
]
