"""会话的归属与可见性（设计 8.1），以及主人停用之后的运行时规则（设计 7.2 末段）。

会话的主人是 ``SessionRecord.user_id``：建会话时调用者代表的员工（浏览器登录或被代调用）；API key
直接建的会话没有主人。

**可见范围**按调用者分三种，规则只写在这里：

| 调用者 | 能看见的会话 |
| --- | --- |
| 普通成员：代表员工（浏览器登录或被代调用），生效角色 ``tenant_member`` | 只有这名员工自己的 |
| 管理员：代表员工，生效角色 ``tenant_admin`` | 本租户全部 |
| 不代表员工的 API key（不论角色） | 本租户全部（与 P1 之前相同） |

被代调用时生效角色取 key 与员工中较低的那个（``tenancy.lower_role``），所以管理员的 key 代成员调用、
成员的 key 代管理员调用，都只看见被代的这名员工自己的会话。

会话的每个入口——取、关、发消息、送审批、停止、回放、待办——都经 ``SessionOwnership.visible``
取会话（继续对话的两个经 ``writable``，它先调 ``visible``），列会话经 ``list_sessions``。看不见的
会话与不存在的是同一个 ``404``（同一个错误码、同一句提示），不透露它是否存在。

**列会话**：普通成员总是只列自己的，``mine`` 被忽略——``mine=false`` 也只列自己的，不报错（控制台
等调用方默认不带 ``mine``，拒绝它会让这些调用方对成员直接失效）。其余调用者 ``mine=true`` 时：代表
员工的按员工过滤（同一名员工经浏览器、经代调用建的会话都算），不代表员工的 key 按创建者
（``created_by``，与 P1 之前相同）。

**主人停用**：会话的主人已停用时，任何调用者（包括管理员与系统 key）都不能再往这个会话发消息、送
审批结果，``403 session_owner_disabled``。这样停用流水线里「关闭他名下的会话」那一步即使没执行完，
停用员工的会话也不会再往下跑。停止生成放行：它只会让会话更快停下；关闭会话放行；只读的取、回放、
待办也照常。主人状态每次都查身份存储、不缓存，停用立即生效，重新启用也立即放行。没有主人的会话
不受这条约束。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from xuanpu_server.errors import (
    ConflictError,
    NotFoundError,
    PlatformError,
    SessionOwnerDisabledError,
)

if TYPE_CHECKING:
    from xuanpu_server.controlplane.models import SessionRecord
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.identity.store import IdentityStore
    from xuanpu_server.tenancy import Principal

logger = logging.getLogger(__name__)


def own_sessions_only(principal: Principal) -> bool:
    """调用者是否只能看见自己的会话：代表某名员工，且生效角色是普通成员。"""
    return principal.user_id is not None and principal.role == "tenant_member"


def can_see(principal: Principal, record: SessionRecord) -> bool:
    """调用者能否看见这个会话。租户边界由取会话时按调用者的租户过滤保证，这里只管员工这一层。"""
    return not own_sessions_only(principal) or record.user_id == principal.user_id


def require_active(record: SessionRecord) -> None:
    """会话已关闭就不能再开引擎、再对话。"""
    if record.status != "active":
        raise ConflictError(f"会话 {record.session_id!r} 已关闭")


class SessionOwnership:
    """会话入口的可见性检查与主人状态检查。"""

    def __init__(self, store: ControlPlaneStore, users: IdentityStore) -> None:
        """
        Args:
            store: 控制面存储（会话记录）。
            users: 身份存储（会话主人的状态）。
        """
        self._store = store
        self._users = users

    async def visible(self, principal: Principal, session_id: str) -> SessionRecord:
        """取一个调用者看得见的会话。别的租户的、看不见的，都按不存在处理。

        Raises:
            NotFoundError: 会话不存在、属于别的租户，或不归这名普通成员。
        """
        record = await self._store.get_session(principal.require_tenant(), session_id)
        if record is None or not can_see(principal, record):
            raise NotFoundError(f"会话 {session_id!r} 不存在")
        return record

    async def writable(self, principal: Principal, session_id: str) -> SessionRecord:
        """取一个可以继续对话（发消息、送审批结果）的会话：看得见、没关闭、主人没停用。

        Raises:
            NotFoundError: 见 ``visible``。
            ConflictError: 会话已关闭。
            SessionOwnerDisabledError: 会话的主人已停用。
        """
        record = await self.visible(principal, session_id)
        require_active(record)
        await self.require_owner_active(record)
        return record

    async def require_owner_active(self, record: SessionRecord) -> None:
        """会话有主人的，主人必须是启用状态。每次都查身份存储，不缓存。

        Raises:
            SessionOwnerDisabledError: 主人已停用。
            PlatformError: 身份存储里查不到主人（员工记录不会删除，这是存储出了问题；按内部
                错误处理，细节只进日志，不当作启用放行）。
        """
        if record.user_id is None:
            return
        user = await self._users.get_user(record.tenant_id, record.user_id)
        if user is None:
            logger.error("会话 %s 的主人 %s 在身份存储里查不到", record.session_id, record.user_id)
            raise PlatformError("服务内部错误")
        if user.status != "active":
            raise SessionOwnerDisabledError(
                f"会话 {record.session_id!r} 的主人已停用：不能再发消息或送审批结果"
                "（停止生成、关闭会话不受影响）"
            )

    async def list_sessions(
        self, principal: Principal, *, status: str | None, mine: bool, limit: int
    ) -> list[SessionRecord]:
        """列出调用者看得见的会话，最近活动的在前。``mine`` 的含义见模块说明。"""
        by_user = own_sessions_only(principal) or (mine and principal.user_id is not None)
        by_creator = mine and principal.user_id is None
        return await self._store.list_sessions(
            principal.require_tenant(),
            status=status,
            created_by=principal.actor if by_creator else None,
            user_id=principal.user_id if by_user else None,
            limit=limit,
        )


__all__ = ["SessionOwnership", "can_see", "own_sessions_only", "require_active"]
