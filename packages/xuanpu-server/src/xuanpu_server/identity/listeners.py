"""P1 登记的「员工停用」监听者：关闭员工名下全部 active 会话（设计 7.2 第 3 步）。

**依赖方向**：身份域不依赖 ``runtime``。会话管理经 ``SessionCloser`` 协议（结构化类型）由装配处
（``Platform``）注入，本模块不 import ``runtime``；列会话用控制面存储。

**以谁的身份关**：``system_principal(租户, CLEANUP_ACTOR)``——角色 ``tenant_admin``、只在本租户、不
代表员工（所以看得见本租户全部会话）；只在平台内部构造，不经任何接口签发。审计 ``session_closed``
的 ``actor`` 是 ``system:user-disabled``，``user_id`` 与会话里别的记录一样是会话主人（被停用的
员工）。

**会话被别的实例持有时**：P0 的 ``SessionManager.close`` 不需要持有租约——它先放掉本实例上的引擎
（有的话：取消在途对话、关事件枢纽、放租约），再把控制面里的会话记录标成 ``closed``、记
``session_closed``。别的实例持有的会话照样被标 ``closed``，立即生效：之后任何实例都不再接受它的
消息与审批结果（``409 conflict``；先于这一步，主人停用已经让它们是 ``403``）。持有它的那个实例不会
被通知：正在跑的那一轮不被取消、跑完为止（要审批的写操作停在审批点，审批结果送不回来），引擎随后按
空闲释放（``limits.session_idle_seconds``）或在停机时释放。这与在别的实例上调
``DELETE /v1/sessions/{id}`` 是同一行为。

**翻页**：控制面的 ``list_sessions`` 没有游标（也不为此改它）。关掉的会话不再是 active，所以反复取
「仍是 active 的前 N 个」，直到取不出没试过的为止；关不掉的仍是 active，每轮多取同样多个，保证没试过
的总有 N 个名额（``close`` 报错、或返回了但会话仍不是 ``closed`` 的都算关不掉）。每个会话只试一次；
全部试过之后有关不掉的，抛 ``SessionCleanupError``（流水线据此记
``user_cleanup_failed``），已关掉的不回滚，重跑时只剩没关掉的。员工已停用，不会再有他的新会话（代他
调用、浏览器登录都被拒），所以循环会结束。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from xuanpu_server.tenancy import system_principal

if TYPE_CHECKING:
    from xuanpu_server.controlplane.models import SessionRecord
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.tenancy import Principal

logger = logging.getLogger(__name__)

CLOSE_SESSIONS = "close_sessions"
"""本监听者登记的名字（``user_cleanup_failed`` 里的 ``listener``）。"""
CLEANUP_ACTOR = "user-disabled"
"""关会话时系统调用者的名字（审计里 ``actor`` 是 ``system:user-disabled``）。"""
PAGE_SIZE = 100
"""每轮最多取多少个仍是 active 的会话（另加上关不掉的个数）。"""


class SessionCloser(Protocol):
    """关闭会话的能力：``runtime.sessions.SessionManager`` 满足它。"""

    async def close(self, principal: Principal, session_id: str) -> SessionRecord:
        """关闭会话（已关闭的原样返回）。"""
        ...


class SessionCleanupError(Exception):
    """有会话没关掉。文本只有个数与会话标识。"""


class CloseUserSessions:
    """关闭员工名下全部 active 会话（等同于强制关闭；正在进行的对话随之取消）。"""

    def __init__(
        self, *, store: ControlPlaneStore, sessions: SessionCloser, page_size: int = PAGE_SIZE
    ) -> None:
        """
        Args:
            store: 控制面存储（按员工列会话）。
            sessions: 关会话的入口（``SessionManager``，走 P0 的收尾）。
            page_size: 每轮最多取多少个没试过的会话。
        """
        self._store = store
        self._sessions = sessions
        self._page_size = page_size

    async def on_user_disabled(self, tenant_id: str, user_id: str) -> None:
        """逐个关掉他名下仍是 active 的会话；关不掉的跳过、接着关别的，最后一并报错。

        Raises:
            SessionCleanupError: 有会话没关掉（已关掉的不回滚）。
        """
        principal = system_principal(tenant_id, CLEANUP_ACTOR)
        tried: set[str] = set()
        failed: list[str] = []
        while True:
            page = await self._store.list_sessions(
                tenant_id, status="active", user_id=user_id, limit=self._page_size + len(failed)
            )
            todo = [record.session_id for record in page if record.session_id not in tried]
            if not todo:
                break
            for session_id in todo:
                tried.add(session_id)
                if not await self._close_one(principal, user_id, session_id):
                    failed.append(session_id)
        if failed:
            raise SessionCleanupError(f"{len(failed)} 个会话没有关掉：{', '.join(sorted(failed))}")
        if tried:
            logger.info("员工 %s（租户 %s）停用后关掉了 %d 个会话", user_id, tenant_id, len(tried))

    async def _close_one(self, principal: Principal, user_id: str, session_id: str) -> bool:
        """关一个会话，返回是否确实关掉了。

        ``close`` 报错算没关掉；返回了、但记录仍不是 ``closed`` 的也算没关掉（不靠「关不掉就会报错」
        这个前提：它仍是 active，下一轮列会话还会取到它，翻页靠的正是把它记进失败的个数）。
        """
        try:
            record = await self._sessions.close(principal, session_id)
        except Exception:
            logger.exception("员工 %s 停用后关会话 %s 失败", user_id, session_id)
            return False
        if record.status != "closed":
            logger.warning(
                "员工 %s 停用后关会话 %s：关闭返回了，会话却仍是 %s",
                user_id,
                session_id,
                record.status,
            )
            return False
        return True


__all__ = [
    "CLEANUP_ACTOR",
    "CLOSE_SESSIONS",
    "PAGE_SIZE",
    "CloseUserSessions",
    "SessionCleanupError",
    "SessionCloser",
]
