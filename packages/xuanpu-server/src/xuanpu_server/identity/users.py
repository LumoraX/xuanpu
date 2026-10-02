"""用户管理（设计第 9 节）：列员工、取员工、改角色、停用与启用。

| 操作 | 谁能做 |
| --- | --- |
| 列员工 | 租户管理员 |
| 取员工 | 租户管理员，或员工本人（浏览器登录或被代调用）；别人的与不存在的同一个 404 |
| 改角色、停用、启用 | 租户管理员 |

**保住最后一名管理员**：经这里改角色、停用都传 ``keep_an_admin=True``：这次改动会让租户里一名启用的
管理员都不剩时，存储原子地拒绝（``409 conflict``，提示说明原因），两名管理员同时互相降级至多一个
成功。
飞书离职事件与对账停用不经这里、不保（离职的管理员必须停用），兜底是平台管理员签发一把租户管理员的
API key，再经 ``PUT /v1/users/{id}/role`` 重新指定（运维手册）。

**审计**：``user_role_changed``（``before``、``after``；改成原来的角色不记）、``user_disabled``（经停用
流水线，原因 ``admin``）、``user_enabled``（``previous_reason``：停用时的原因；已是启用的不记）。
``user_id`` 都是被操作的员工，``actor`` 是调用者。

启用只改状态：已关闭的会话不恢复，删掉的登录会话也不恢复（员工重新登录即可）。员工在飞书里仍是离职
状态的，下一次对账会再把他停用。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from xuanpu_server.errors import NotFoundError, PlatformError

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.identity.disable import DisablePipeline
    from xuanpu_server.identity.models import UserQuery, UserRecord, UserStatus
    from xuanpu_server.identity.store import IdentityStore
    from xuanpu_server.tenancy import Principal, TenantRole

logger = logging.getLogger(__name__)


def _missing(user_id: str) -> NotFoundError:
    """员工不存在（或调用者看不见他）时的错误：两种情况同一句提示。"""
    return NotFoundError(f"用户 {user_id!r} 不存在")


class UserAdmin:
    """用户管理。由 ``IdentityService`` 组合，接入层经 ``IdentityService.users`` 调用。"""

    def __init__(
        self,
        *,
        store: IdentityStore,
        disabling: DisablePipeline,
        audit: AuditLog,
        now: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            store: 身份存储。
            disabling: 停用流水线（停用走它）。
            audit: 审计写入口。
            now: 取当前时间的函数，测试时可替换。
        """
        self._store = store
        self._disabling = disabling
        self._audit = audit
        self._now = now

    async def list_users(self, principal: Principal, query: UserQuery) -> list[UserRecord]:
        """列出本租户的员工（租户管理员）。

        Raises:
            PermissionDeniedError: 不是租户管理员。
        """
        return await self._store.list_users(principal.require_tenant_admin(), query)

    async def get_user(self, principal: Principal, user_id: str) -> UserRecord:
        """取一名员工：租户管理员取谁都行，别人只能取自己。

        Raises:
            NotFoundError: 本租户没有这名员工，或调用者不是管理员、也不是他本人（同一句提示）。
        """
        tenant_id = principal.require_tenant()
        if principal.role != "tenant_admin" and principal.user_id != user_id:
            raise _missing(user_id)
        user = await self._store.get_user(tenant_id, user_id)
        if user is None:
            raise _missing(user_id)
        return user

    async def set_role(self, principal: Principal, user_id: str, role: TenantRole) -> UserRecord:
        """改角色（租户管理员），保住最后一名启用的管理员；角色真的变了才记 ``user_role_changed``。

        Raises:
            PermissionDeniedError: 不是租户管理员。
            NotFoundError: 本租户没有这名员工。
            ConflictError: 这是最后一名启用的管理员，不能降为成员。
        """
        tenant_id = principal.require_tenant_admin()
        current = await self._store.get_user(tenant_id, user_id)
        if current is None:
            raise _missing(user_id)
        if current.role == role:
            return current
        updated = await self._store.set_user_role(tenant_id, user_id, role, keep_an_admin=True)
        await self._audit.control(
            principal,
            tenant_id,
            "user_role_changed",
            {"before": current.role, "after": role},
            user_id=user_id,
        )
        return updated

    async def set_status(
        self, principal: Principal, user_id: str, status: UserStatus
    ) -> tuple[UserRecord, tuple[str, ...]]:
        """停用或启用（租户管理员），返回 ``(改后的员工, 失败的收尾)``。

        停用走停用流水线（原因 ``admin``、三步都做完、保住最后一名管理员；对已停用的员工会重跑
        收尾）；启用只改状态（见 ``enable``）。

        Raises:
            PermissionDeniedError: 不是租户管理员。
            NotFoundError: 本租户没有这名员工。
            ConflictError: 停用的是最后一名启用的管理员。
        """
        tenant_id = principal.require_tenant_admin()
        if status == "disabled":
            outcome = await self._disabling.disable(
                tenant_id, user_id, "admin", principal, keep_an_admin=True
            )
            return outcome.user, outcome.cleanup_failed
        return await self.enable(tenant_id, user_id, principal), ()

    async def enable(self, tenant_id: str, user_id: str, actor: Principal) -> UserRecord:
        """重新启用：只改状态，状态真的变了才记 ``user_enabled``。不恢复已关闭的会话。

        Raises:
            NotFoundError: 本租户没有这名员工。
        """
        current = await self._store.get_user(tenant_id, user_id)
        if current is None:
            raise _missing(user_id)
        # 启用时存储不落 at（只有停用记时间），照常给当前时间
        changed = await self._store.set_user_status(tenant_id, user_id, "active", at=self._now())
        if changed:
            await self._audit.control(
                actor,
                tenant_id,
                "user_enabled",
                {"previous_reason": current.disabled_reason},
                user_id=user_id,
            )
        updated = await self._store.get_user(tenant_id, user_id)
        if updated is None:
            logger.error("员工 %s（租户 %s）刚启用，身份存储里却查不到", user_id, tenant_id)
            raise PlatformError("服务内部错误")
        return updated


__all__ = ["UserAdmin"]
