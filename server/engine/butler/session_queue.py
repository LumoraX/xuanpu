"""同员工主会话顺序执行队列与三阶段安全强制插入合并（ADR 0024）。

- 保留「输入框永不禁用」体验，同时在开启顺序模式（或显式传入 ``mode=queue`` /
  ``mode=interrupt_merge``）时避免同员工连发碎片造成的脏写、重复建人与漏关联。
- 默认顺序排队（FIFO）：前轮进行中时，新消息先落盘原文并返回 ``state=queued``，
  前轮收口后按序启动。
- 强制插入合并（``interrupt_merge``）：按当前轮次副作用阶段分级处理：
  1. 阶段 1（无写副作用、无派工）：直接取消并合并原文与附件重跑；
  2. 阶段 2（仅有可撤回私域写入）：倒序执行 ``undo_executed_action`` 确认回滚后合并重跑；
  3. 阶段 3（已派出匠人/编组或含不可撤回写入）：保护性合并，不静默抛弃在途派工，
     把在途摘要注入合并轮次交由管家显式处置。
- 防饿死上限：单次连续合并达到 ``max_merges`` 后自动降级为 FIFO 排队。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from server.engine.butler.personal_actions import undo_executed_action

if TYPE_CHECKING:
    from collections.abc import Callable

    from server.core.tenancy import Principal
    from server.engine.butler.turn import ButlerTurn
    from server.platform import Platform

logger = logging.getLogger(__name__)
MessageMode = Literal["default", "queue", "interrupt_merge"]
InterruptStage = Literal["clean_cancel", "undo_and_merge", "protective_merge"]


@dataclass
class MergePayload:
    """合并重跑时传递给新轮次的上下文。"""

    merged_text: str
    merged_request_ids: tuple[str, ...]
    merge_count: int
    stage: InterruptStage
    prior_in_flight: dict[str, Any] | None = None


@dataclass
class ActiveSessionSlot:
    """同一员工主会话当前正在执行的槽位。"""

    user_key: str
    request_id: str
    text: str
    who: Principal
    task: asyncio.Task[dict[str, Any]]
    on_superseded: Callable[[str, tuple[str, ...]], None]
    turn: ButlerTurn | None = None
    merged_request_ids: tuple[str, ...] = ()
    merge_count: int = 0
    superseded_by: str | None = None


@dataclass
class QueuedItem:
    """FIFO 队列中的一条待执行消息。"""

    request_id: str
    starter: Callable[[], asyncio.Task[dict[str, Any]]]
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    started_task: asyncio.Task[dict[str, Any]] | None = None


def classify_stage(turn: ButlerTurn | None) -> InterruptStage:
    """按当前轮次已发生的副作用判定强制插入合并阶段。"""
    if turn is None:
        return "clean_cancel"
    if turn.dispatches or turn.groups or turn.answered or turn.proposal is not None:
        return "protective_merge"
    if turn.executed_actions:
        return "undo_and_merge"
    return "clean_cancel"


async def rollback_actions(
    platform: Platform, who: Principal, actions: list[dict[str, Any]],
) -> tuple[bool, list[dict[str, Any]]]:
    """倒序撤回当前轮次已执行的私域动作；返回 ``(是否全部成功, 未能撤回的动作列表)``。"""
    remaining: list[dict[str, Any]] = []
    for action in reversed(actions):
        try:
            await undo_executed_action(platform, who, action)
        except Exception:
            logger.exception("强制插入合并阶段 2 撤回动作失败，降级为保护性合并")
            remaining.append(action)
    return not remaining, remaining


class SessionQueueCoordinator:
    """按 ``(tenant_id, user_id)`` 协调主会话 FIFO 排队与强制插入合并。"""

    def __init__(self, platform: Platform, *, max_merges: Callable[[], int]) -> None:
        """依赖由管家服务注入。"""
        self._platform = platform
        self._max_merges = max_merges
        self._active: dict[str, ActiveSessionSlot] = {}
        self._queues: dict[str, deque[QueuedItem]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, user_key: str) -> asyncio.Lock:
        """取单个员工的协调锁。"""
        lock = self._locks.get(user_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_key] = lock
        return lock

    def active_slot(self, user_key: str) -> ActiveSessionSlot | None:
        """当前该员工正在运行的槽位（无则返回 None）。"""
        slot = self._active.get(user_key)
        if slot is not None and slot.task.done():
            self._active.pop(user_key, None)
            return None
        return slot

    def bind_turn(self, user_key: str, request_id: str, turn: ButlerTurn) -> None:
        """将已创建的 ``ButlerTurn`` 挂到活跃槽位，供强制插入阶段检测。"""
        slot = self._active.get(user_key)
        if slot is not None and slot.request_id == request_id:
            slot.turn = turn

    def register_active(self, slot: ActiveSessionSlot) -> None:
        """登记当前活跃槽位，并在其结束后自动拉起 FIFO 队头。"""
        self._active[slot.user_key] = slot

        def _on_done(_task: asyncio.Task[dict[str, Any]]) -> None:
            if self._active.get(slot.user_key) is slot:
                self._active.pop(slot.user_key, None)
                self._pump_next(slot.user_key)

        slot.task.add_done_callback(_on_done)

    def _pump_next(self, user_key: str) -> None:
        """当前轮次结束后，若 FIFO 队列非空则立即启动队头。"""
        queue = self._queues.get(user_key)
        if not queue:
            self._queues.pop(user_key, None)
            return
        nxt = queue.popleft()
        nxt.started_task = nxt.starter()
        nxt.ready.set()

    async def enqueue(
        self, user_key: str, request_id: str,
        starter: Callable[[], asyncio.Task[dict[str, Any]]],
    ) -> tuple[QueuedItem, int]:
        """将请求排入该员工的 FIFO 队列，返回 ``(条目, 排队位次从 1 起)``。"""
        async with self._lock_for(user_key):
            queue = self._queues.setdefault(user_key, deque())
            item = QueuedItem(request_id=request_id, starter=starter)
            queue.append(item)
            return item, len(queue)

    async def interrupt_and_prepare_merge(
        self, user_key: str, new_request_id: str, new_text: str,
    ) -> MergePayload | None:
        """尝试中断当前活跃轮次并构造合并载荷；无活跃轮次或已达合并上限时返回 None。"""
        async with self._lock_for(user_key):
            slot = self.active_slot(user_key)
            if slot is None:
                return None
            if slot.merge_count >= self._max_merges():
                return None
            prior_ids = slot.merged_request_ids or (slot.request_id,)
            merged_ids = (*prior_ids, new_request_id)
            stage = classify_stage(slot.turn)
            slot.superseded_by = new_request_id
            slot.on_superseded(new_request_id, merged_ids)
            slot.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await slot.task
            if self._active.get(user_key) is slot:
                self._active.pop(user_key, None)

            prior_in_flight: dict[str, Any] | None = None
            if stage == "undo_and_merge" and slot.turn is not None:
                all_ok, remaining = await rollback_actions(
                    self._platform, slot.who, list(slot.turn.executed_actions))
                if not all_ok:
                    stage = "protective_merge"
                    prior_in_flight = {"remaining_actions": remaining}
            elif stage == "protective_merge" and slot.turn is not None:
                prior_in_flight = {
                    "dispatches": [
                        {"dispatch_id": d.dispatch_id, "package": d.artisan.package,
                         "entry": d.artisan.entry, "state": d.state}
                        for d in slot.turn.dispatches
                    ],
                    "groups": list(slot.turn.groups),
                    "executed_actions": list(slot.turn.executed_actions),
                }

            merged_text = f"{slot.text}\n{new_text}"
            return MergePayload(
                merged_text=merged_text,
                merged_request_ids=merged_ids,
                merge_count=slot.merge_count + 1,
                stage=stage,
                prior_in_flight=prior_in_flight,
            )

    async def close(self) -> None:
        """清空所有队列并唤醒等待方。"""
        for queue in self._queues.values():
            for item in queue:
                item.ready.set()
        self._queues.clear()
        self._active.clear()
