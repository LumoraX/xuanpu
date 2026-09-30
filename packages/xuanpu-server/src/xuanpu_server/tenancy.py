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

Role = Literal["platform_admin", "tenant_admin", "tenant_member"]
TenantStatus = Literal["active", "suspended"]

API_KEY_PREFIX = "xp"
ROOT_KEY_PREFIX = "xproot"

_TENANT_ID = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")
_KEY_ID = re.compile(r"^[0-9a-f]{12}$")


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
    """一把 API key 的存档。只存密钥的摘要，不存明文。"""

    key_id: str
    tenant_id: str
    name: str
    role: Role
    secret_hash: str
    created_at: float
    revoked_at: float | None = None

    @property
    def active(self) -> bool:
        """是否仍然有效。"""
        return self.revoked_at is None


@dataclass(frozen=True)
class Principal:
    """一次调用的调用者。

    Attributes:
        role: 角色。
        tenant_id: 所属租户；平台管理员不属于任何租户，为 None。
        key_id: 所用 API key 的标识；根凭据为 ``"root"``。
    """

    role: Role
    tenant_id: str | None
    key_id: str

    @property
    def actor(self) -> str:
        """写进审计记录的调用者标识。"""
        return f"key:{self.key_id}"

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


def validate_tenant_id(tenant_id: str) -> str:
    """租户标识会出现在目录名、表名前缀里，字符集必须受限。"""
    if not _TENANT_ID.match(tenant_id):
        raise ValueError(
            f"租户标识非法：{tenant_id!r}（3–40 个字符，小写字母开头，只含小写字母、数字、连字符）"
        )
    return tenant_id


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


def verify_root_key(presented: str, root_key_hash: str) -> bool:
    """校验平台根凭据。"""
    prefix, _, secret = presented.partition("_")
    if prefix != ROOT_KEY_PREFIX or not secret:
        return False
    return verify_secret(secret, root_key_hash)


__all__ = [
    "API_KEY_PREFIX",
    "ROOT_KEY_PREFIX",
    "ApiKeyRecord",
    "Principal",
    "QuotaLimits",
    "Role",
    "Tenant",
    "TenantStatus",
    "generate_api_key",
    "generate_root_key",
    "hash_secret",
    "parse_api_key",
    "validate_tenant_id",
    "verify_root_key",
    "verify_secret",
]
