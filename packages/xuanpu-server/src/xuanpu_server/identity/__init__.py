"""身份域：平台用户、外部身份（飞书）、登录会话与登录尝试、按用户用量、对账状态。

依赖方向：本包只依赖 ``ids``、``errors``、``tenancy``、``secrets``、``audit`` 与控制面（内存实现
按控制面判断租户是否存在；Postgres 实现共用其命名空间与迁移），不依赖 ``runtime``。用户的角色
沿用 ``tenancy.TenantRole``。Postgres 实现在 ``identity.postgres``，需要 ``postgres`` 可选依赖，
这里不导出。``identity.config`` 是服务配置的 ``identity`` 段，由 ``xuanpu_server.config`` 引用
（不反过来引用服务配置）。飞书接口的客户端在 ``identity.feishu``，登录流程在 ``identity.login``
（几个地址在 ``identity.urls``，调飞书的并发上限在 ``identity.limits``），周期清理在
``identity.maintenance``；员工停用流水线在 ``identity.disable``（P1 的监听者在
``identity.listeners``，经协议接会话管理，由 ``Platform`` 登记），用户管理在 ``identity.users``，
飞书事件回调的纯计算在 ``identity.events``、编排在 ``identity.callback``。都经
``identity.service.IdentityService`` 对外。
"""

from __future__ import annotations

from xuanpu_server.identity.memory import InMemoryIdentityStore
from xuanpu_server.identity.models import (
    DisabledReason,
    ExternalIdentity,
    IdentityProvider,
    LoginAttempt,
    LoginFailureReason,
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
    "LoginFailureReason",
    "LoginSession",
    "ReconcileState",
    "TenantIdentityConfig",
    "UserQuery",
    "UserRecord",
    "UserStatus",
    "UserUsageRecord",
    "new_user_id",
]
