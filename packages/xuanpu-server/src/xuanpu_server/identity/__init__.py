"""身份域：平台用户、外部身份（飞书）、登录会话与登录尝试、按用户用量、对账状态。

依赖方向：本包只依赖 ``ids``、``errors``、``tenancy`` 与控制面（内存实现按控制面判断租户是否
存在；Postgres 实现共用其命名空间与迁移），不依赖 ``runtime``。用户的角色沿用
``tenancy.TenantRole``。Postgres 实现在 ``identity.postgres``，需要 ``postgres`` 可选依赖，
这里不导出。
"""

from __future__ import annotations

from xuanpu_server.identity.memory import InMemoryIdentityStore
from xuanpu_server.identity.models import (
    DisabledReason,
    ExternalIdentity,
    IdentityProvider,
    LoginAttempt,
    LoginSession,
    ReconcileState,
    TenantIdentityConfig,
    UserQuery,
    UserRecord,
    UserStatus,
    UserUsageRecord,
    new_user_id,
)
from xuanpu_server.identity.store import IdentityStore

__all__ = [
    "DisabledReason",
    "ExternalIdentity",
    "IdentityProvider",
    "IdentityStore",
    "InMemoryIdentityStore",
    "LoginAttempt",
    "LoginSession",
    "ReconcileState",
    "TenantIdentityConfig",
    "UserQuery",
    "UserRecord",
    "UserStatus",
    "UserUsageRecord",
    "new_user_id",
]
