"""员工停用流水线（设计 7.2）与「员工停用」监听者的登记。

管理员接口（``IdentityService.disable_user``，经 ``identity.users``）、飞书离职事件
（``identity.callback``）以及以后的对账，都经 ``DisablePipeline`` 停用员工：

1. 标停用，记下原因与时间（``keep_an_admin`` 为真时不停用最后一名启用的管理员）。状态真的变了才记
   一条 ``user_disabled``、计一次 ``xuanpu_users_disabled_total{reason}``；已是停用的不改（原因与
   时间保持第一次的），也不再记；
2. 删他的全部登录会话（认证每个请求都查员工状态，停用后 cookie 本来就是 401；这一步把库里的记录
   清掉）；
3. 依次通知登记的监听者（``on_user_disabled(tenant_id, user_id)``，必须能重复执行）。某个失败时记
   ``user_cleanup_failed``（监听者名与异常类型，不带异常文本：它可能带着内部地址之类的东西，细节只进
   日志），接着通知其余的。

**重复停用重跑第 3 步**：对已停用的员工再停用一次，第 1、2 步什么也不变，第 3 步全部重跑——管理员
据此重试失败的收尾。

**谁做的**：``actor`` 是一个 ``Principal``——管理员接口取调用者；飞书离职事件取
``system_principal(租户, FEISHU_EVENT_ACTOR)``（审计里是 ``system:feishu-event``）；对账（P1
Task 7）取 ``system:reconcile``。``user_disabled``、``user_cleanup_failed`` 的 ``user_id`` 是被停用
的员工。

**同步与后台**：管理员接口三步都做完再返回（``disable``）。飞书要 3 秒内回 200，所以事件回调同步做第
1、2 步（``mark_disabled``），第 3 步交给 ``notify_later`` 放后台：任务引用存在本对象里（asyncio 只
弱引用任务，不存可能被回收），结束时取走异常记日志；``close`` 时等它们最多 ``drain_seconds`` 秒，
还没完的取消并记告警，之后不再接新的后台任务。被取消的、进程崩溃时丢了的，有会话主人停用的运行时
规则兜底（``runtime.ownership``：主人停用的会话不能再发消息、送审批），空闲会话照常回收；管理员再
停用一次就会重跑。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from xuanpu_server.errors import PlatformError

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.identity.models import DisabledReason, UserRecord
    from xuanpu_server.identity.store import IdentityStore
    from xuanpu_server.metrics import PlatformMetrics
    from xuanpu_server.tenancy import Principal

logger = logging.getLogger(__name__)

FEISHU_EVENT_ACTOR = "feishu-event"
"""飞书离职事件停用员工时，系统调用者的名字（审计里 ``actor`` 是 ``system:feishu-event``）。"""
DRAIN_SECONDS = 5.0
"""平台关闭时最多等后台的收尾这么久（秒），还没完的取消。"""


class UserDisabledListener(Protocol):
    """「员工停用」监听者：员工停用之后要做的收尾（P1 关会话；以后 P3 冻结空间、P6 撤销令牌）。

    必须能重复执行：同一名员工可能被通知多次（重复停用、飞书重推、后台任务被取消后重跑）。
    """

    async def on_user_disabled(self, tenant_id: str, user_id: str) -> None:
        """员工已停用（第 1、2 步已做完）：做这一项收尾。失败就抛异常，流水线记审计。"""
        ...


@dataclass(frozen=True)
class DisableOutcome:
    """一次停用的结果。

    Attributes:
        user: 停用之后的员工记录。
        changed: 状态是不是这一次才从启用变成停用的。
        cleanup_failed: 失败的监听者名（按登记顺序）；第 3 步没做或全部成功为空。
    """

    user: UserRecord
    changed: bool
    cleanup_failed: tuple[str, ...] = ()


class DisablePipeline:
    """员工停用流水线。一个 ``IdentityService`` 一份；监听者由装配处（``Platform``）登记。"""

    def __init__(
        self,
        *,
        store: IdentityStore,
        audit: AuditLog,
        metrics: PlatformMetrics | None = None,
        now: Callable[[], float] = time.time,
        drain_seconds: float = DRAIN_SECONDS,
    ) -> None:
        """
        Args:
            store: 身份存储。
            audit: 审计写入口。
            metrics: 平台指标；None 不打点。
            now: 取当前时间的函数，测试时可替换。
            drain_seconds: ``close`` 时最多等后台的收尾多久。
        """
        self._store = store
        self._audit = audit
        self._metrics = metrics
        self._now = now
        self._drain_seconds = drain_seconds
        self._listeners: list[tuple[str, UserDisabledListener]] = []
        self._tasks: set[asyncio.Task[tuple[str, ...]]] = set()
        self._closed = False

    def register(self, name: str, listener: UserDisabledListener) -> None:
        """登记一个监听者，按登记顺序通知。名字写进审计，不能重复。

        Raises:
            ValueError: 这个名字已经登记过。
        """
        if any(existing == name for existing, _ in self._listeners):
            raise ValueError(f"监听者 {name!r} 已经登记过")
        self._listeners.append((name, listener))

    @property
    def pending(self) -> int:
        """后台还没结束的收尾任务数。"""
        return len(self._tasks)

    async def disable(
        self,
        tenant_id: str,
        user_id: str,
        reason: DisabledReason,
        actor: Principal,
        *,
        keep_an_admin: bool,
        detail: dict[str, Any] | None = None,
    ) -> DisableOutcome:
        """停用员工，三步都做完再返回。参数与异常同 ``mark_disabled``。"""
        marked = await self.mark_disabled(
            tenant_id, user_id, reason, actor, keep_an_admin=keep_an_admin, detail=detail
        )
        failed = await self.notify(tenant_id, user_id, actor)
        return dataclasses.replace(marked, cleanup_failed=failed)

    async def mark_disabled(
        self,
        tenant_id: str,
        user_id: str,
        reason: DisabledReason,
        actor: Principal,
        *,
        keep_an_admin: bool,
        detail: dict[str, Any] | None = None,
    ) -> DisableOutcome:
        """第 1、2 步：标停用（真的变了才记审计、计数），删他的全部登录会话。

        Args:
            reason: 停用原因。
            actor: 谁停用的（管理员，或系统调用者）。
            keep_an_admin: 为真时不停用最后一名启用的管理员（管理员接口）；离职事件与对账为假。
            detail: 另外写进 ``user_disabled`` 的内容（飞书事件的 ``event_id``）。

        Raises:
            NotFoundError: 本租户没有这名员工。
            ConflictError: ``keep_an_admin`` 为真且这是最后一名启用的管理员。
        """
        changed = await self._store.set_user_status(
            tenant_id,
            user_id,
            "disabled",
            reason=reason,
            at=self._now(),
            keep_an_admin=keep_an_admin,
        )
        if changed:
            data = {"reason": reason, **(detail or {})}
            await self._audit.control(actor, tenant_id, "user_disabled", data, user_id=user_id)
            if self._metrics is not None:
                self._metrics.user_disabled(reason)
        removed = await self._store.delete_user_login_sessions(tenant_id, user_id)
        user = await self._store.get_user(tenant_id, user_id)
        if user is None:
            # 刚改过状态就查不到（员工记录不会删除）：存储出了问题，按内部错误处理
            logger.error("员工 %s（租户 %s）刚停用，身份存储里却查不到", user_id, tenant_id)
            raise PlatformError("服务内部错误")
        logger.info(
            "员工 %s（租户 %s）停用（%s，原因 %s），删掉 %d 个登录会话",
            user_id,
            tenant_id,
            "新停用" if changed else "已是停用",
            user.disabled_reason,
            removed,
        )
        return DisableOutcome(user=user, changed=changed)

    async def notify(self, tenant_id: str, user_id: str, actor: Principal) -> tuple[str, ...]:
        """第 3 步：依次通知全部监听者，返回失败的那些的名字。

        某个失败时记 ``user_cleanup_failed``，接着通知其余的。这条审计写不进去时也先通知完其余的，
        最后再把写审计的异常抛出去（审计写不进去，操作本身就算失败）。
        """
        failed: list[str] = []
        unrecorded: list[Exception] = []
        for name, listener in list(self._listeners):
            try:
                await listener.on_user_disabled(tenant_id, user_id)
            except Exception as exc:
                logger.exception(
                    "员工 %s（租户 %s）停用后的收尾「%s」失败", user_id, tenant_id, name
                )
                failed.append(name)
                data = {"listener": name, "error": type(exc).__name__}
                try:
                    await self._audit.control(
                        actor, tenant_id, "user_cleanup_failed", data, user_id=user_id
                    )
                except Exception as audit_error:
                    logger.exception("记 user_cleanup_failed 失败（收尾「%s」）", name)
                    unrecorded.append(audit_error)
        if unrecorded:
            raise unrecorded[0]
        return tuple(failed)

    def notify_later(self, tenant_id: str, user_id: str, actor: Principal) -> None:
        """把第 3 步放进后台（飞书事件回调用：响应不等它）。关闭之后不再接，记告警。"""
        if self._closed:
            logger.warning(
                "停用流水线已关闭：员工 %s（租户 %s）停用后的收尾没有放进后台，"
                "靠主人停用的运行时规则兜底，管理员可再停用一次重跑",
                user_id,
                tenant_id,
            )
            return
        task = asyncio.create_task(
            self.notify(tenant_id, user_id, actor), name=f"user-disabled:{tenant_id}:{user_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[tuple[str, ...]]) -> None:
        """后台任务结束：放掉引用，取走异常记日志（监听者的失败已在 ``notify`` 里记过审计）。"""
        self._tasks.discard(task)
        if task.cancelled():
            logger.warning("员工停用后的收尾（%s）被取消，没有跑完", task.get_name())
            return
        error = task.exception()
        if error is not None:
            logger.error("员工停用后的收尾（%s）出错", task.get_name(), exc_info=error)

    async def settle(self) -> None:
        """等后台的收尾全部结束（不论成败）。测试与关闭时用。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        """不再接新的后台任务；等已有的最多 ``drain_seconds`` 秒，还没完的取消。可以重复调用。"""
        self._closed = True
        if not self._tasks:
            return
        _, unfinished = await asyncio.wait(list(self._tasks), timeout=self._drain_seconds)
        if not unfinished:
            return
        logger.warning(
            "关闭时还有 %d 个员工停用后的收尾没跑完（等了 %.1f 秒），已取消；"
            "靠主人停用的运行时规则兜底，管理员可再停用一次重跑",
            len(unfinished),
            self._drain_seconds,
        )
        for task in unfinished:
            task.cancel()
        await asyncio.gather(*unfinished, return_exceptions=True)


__all__ = [
    "DRAIN_SECONDS",
    "FEISHU_EVENT_ACTOR",
    "DisableOutcome",
    "DisablePipeline",
    "UserDisabledListener",
]
