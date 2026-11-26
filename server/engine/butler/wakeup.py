"""唤醒（R5-C2）：对账器把匠人宿主上的可观察事实写成持久事件，消费者逐条处理并确认。

- 对账：按开启管家的租户扫进行中的派工。持久任务到终态写 ``finished``，等本人决定写
  ``asked``；会话每结束一轮（审计里有一条 ``turn_text``）写一条 ``finished``，是否需要
  审批在处理时按这一轮的终态判断。事件按 ``(租户, 派工, key)`` 去重。
- 处理：先算出并持久化决定（交付 / 代答转给匠人 / 转问员工 / 等本人审批；先写者为准），
  再按决定执行幂等的副作用，最后确认。进程在任何一步之间被终止，重启后从持久决定续做，
  每条事件恰好产生一次交付或转问。
- 进程内一把锁串行处理；多实例同时处理同一事件时由“决定先写者为准”与幂等副作用兜住。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from server.core.controlplane.models import AuditQuery
from server.core.errors import NotFoundError
from server.core.ids import new_id
from server.core.tenancy import Principal
from server.domains.work_tasks.models import TERMINAL as TASK_TERMINAL
from server.engine.butler.inspector import inspect, session_round, task_snapshot
from server.engine.butler.trace_link import dispatch_span
from server.engine.butler.wake_bus import ConditionalTimerRegistry, WakeSignal
from server.engine.butler.wake_models import ButlerEvent, WakeDecision
from server.engine.tracing.models import TraceOwner
from server.engine.tracing.narration import without_narration

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from server.engine.butler.groups.runner import GroupRunner
    from server.engine.butler.inspector import HostSnapshot
    from server.engine.butler.models import DispatchRecord
    from server.engine.butler.review import Reviewer
    from server.engine.butler.wake_apply import WakeEffects
    from server.engine.butler.wake_bus import WakeBus
    from server.platform import Platform

logger = logging.getLogger(__name__)
_ROUND_PREFIX = "round:"


def principal(tenant_id: str, user_id: str) -> Principal:
    """派工主人的员工身份（后台处理以本人身份读写本人数据）。"""
    return Principal(role="tenant_member", tenant_id=tenant_id, user_id=user_id, key_id=None)


class Wakeup:
    """对账、处理与后台循环。"""

    def __init__(self, platform: Platform, effects: WakeEffects, reviewer: Reviewer | None, *,
                 now: Callable[[], float], groups: GroupRunner | None = None,
                 bus: WakeBus | None = None,
                 timers: ConditionalTimerRegistry | None = None) -> None:
        """依赖由平台注入；``bus`` 启用事件驱动快路径，``timers`` 承载条件超时器。"""
        self._groups = groups
        self._platform = platform
        self._effects = effects
        self._reviewer = reviewer
        self._now = now
        self._bus = bus
        self._timers = timers if timers is not None else ConditionalTimerRegistry()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._seen: set[tuple[str, str]] = set()

    def _at(self) -> datetime:
        """当前时间（UTC）。"""
        return datetime.fromtimestamp(self._now(), UTC)

    def _reconcile_interval(self) -> float:
        """低频对账兜底周期；未单独配置时沿用 ``wake_interval_seconds``。"""
        cfg = self._platform.butler_config
        return cfg.reconcile_interval_seconds if cfg.reconcile_interval_seconds is not None else cfg.wake_interval_seconds

    def arm_deadline(
        self, key: str, deadline: float, callback: Callable[[], Awaitable[None]],
    ) -> None:
        """登记一条条件超时器；目标提前完成时由 ``cancel_deadline`` 取消。"""
        self._timers.arm(key, deadline, callback)

    def cancel_deadline(self, key: str) -> bool:
        """取消挂起的条件超时器；返回是否命中。"""
        return self._timers.cancel(key)

    def start(self) -> None:
        """启动后台循环。"""
        self._task = asyncio.ensure_future(self._loop())

    async def close(self) -> None:
        """停后台循环。"""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        """事件驱动快路径 + 低频对账兜底；未配总线时退回定时对账。"""
        if self._bus is None:
            while True:
                wait = self._timers.next_wait(self._now(), max_wait=self._reconcile_interval())
                await asyncio.sleep(wait)
                try:
                    await self._timers.fire_due(self._now())
                    await self.run_once()
                except Exception:
                    logger.exception("管家唤醒处理失败，下一轮重试")
        await self._bus.start()
        next_reconcile = self._now() + self._reconcile_interval()
        try:
            while True:
                now = self._now()
                wait = self._timers.next_wait(now, max_wait=max(0.0, next_reconcile - now))
                signals = await self._bus.drain(wait)
                for signal in signals:
                    try:
                        await self.run_signal(signal)
                    except Exception:
                        logger.exception("处理事件唤醒信号 %s 失败，待对账兜底", signal)
                await self._timers.fire_due(self._now())
                if self._now() >= next_reconcile:
                    try:
                        await self.run_once()
                    except Exception:
                        logger.exception("管家定时对账兜底失败，下一轮重试")
                    next_reconcile = self._now() + self._reconcile_interval()
        finally:
            await self._bus.close()

    async def run_signal(self, signal: WakeSignal) -> int:
        """按唤醒信号类型走事件快路径；租户未开启管家直接忽略。"""
        if not self._platform.butler.enabled(signal.tenant_id):
            return 0
        if signal.kind == "dispatch":
            return await self.run_for_dispatch(signal.tenant_id, signal.target_id)
        if signal.kind == "task":
            return await self.run_for_task(signal.tenant_id, signal.target_id)
        if signal.kind == "group":
            return await self.run_for_group(signal.tenant_id, signal.target_id)
        return await self._run_tenant(signal.tenant_id)

    async def run_for_task(self, tenant_id: str, task_id: str) -> int:
        """按工作任务标识反查活跃派工并定点唤醒。"""
        records = await self._platform.dispatches.active(tenant_id)
        matched = next((r for r in records if r.host == "task" and r.host_ref == task_id), None)
        if matched is None:
            return 0
        return await self.run_for_dispatch(tenant_id, matched.dispatch_id)

    async def run_for_dispatch(self, tenant_id: str, dispatch_id: str) -> int:
        """定点对账并处理单条派工的唤醒事件；若属编组立即定点推进编组。"""
        # 若发起该派工的管家首轮仍在收尾，先等其落盘回复与 trace，防止乱序交付与预算漏计
        preview = await self._platform.dispatches.active(tenant_id)
        found = next((r for r in preview if r.dispatch_id == dispatch_id), None)
        if found is not None:
            await self._platform.butler.wait_turn_idle(found.trace_id)
        handled = 0
        async with self._lock:
            records = await self._platform.dispatches.active(tenant_id)
            record = next((r for r in records if r.dispatch_id == dispatch_id), None)
            group_id = record.group_id if record is not None else None
            if record is not None:
                await self._reconcile_record(record)
            for event in await self._platform.store.wake_store.unacked(tenant_id):
                if event.dispatch_id != dispatch_id:
                    continue
                await self.handle(event)
                handled += 1
            if self._groups is not None and group_id is not None:
                await self._groups.advance_group(tenant_id, group_id)
        return handled

    async def run_for_group(self, tenant_id: str, group_id: str) -> int:
        """定点推进租户内指定编组。"""
        if self._groups is None:
            return 0
        async with self._lock:
            advanced = await self._groups.advance_group(tenant_id, group_id)
        return 1 if advanced else 0

    async def _run_tenant(self, tenant: str) -> int:
        """对单个开启管家的租户完整对账一次并处理全部未确认事件。"""
        handled = 0
        async with self._lock:
            if not self._platform.butler.enabled(tenant):
                return 0
            await self._platform.butler.resume_stale(tenant)
            await self.reconcile(tenant)
            for event in await self._platform.store.wake_store.unacked(tenant):
                try:
                    await self.handle(event)
                except Exception:
                    logger.exception("处理唤醒事件 %s 失败，下一轮重试", event.event_id)
                    continue
                handled += 1
            if self._groups is not None:
                await self._groups.advance_tenant(tenant)
        return handled

    async def run_once(self) -> int:
        """对每个开启管家的租户对账一次、处理全部未确认事件；返回处理条数。"""
        handled = 0
        for tenant in self._platform.butler_config.tenants:
            if not self._platform.butler.enabled(tenant):
                continue
            handled += await self._run_tenant(tenant)
        return handled

    async def reconcile(self, tenant: str) -> None:
        """扫进行中的派工，把宿主上的新事实写成事件。"""
        for record in await self._platform.dispatches.active(tenant):
            await self._reconcile_record(record)

    async def _reconcile_record(self, record: DispatchRecord) -> None:
        """对单条活跃派工读宿主状态并写事件；单条失败记日志不阻断其他派工。"""
        if record.state not in ("running", "asking") or record.host_ref is None:
            return
        who = principal(record.tenant_id, record.user_id)
        try:
            if record.host == "task":
                await self._reconcile_task(who, record)
            else:
                await self._reconcile_session(record)
        except Exception:
            logger.exception("对账派工 %s 失败，下一轮重试", record.dispatch_id)

    async def _reconcile_task(self, who: Principal, record: DispatchRecord) -> None:
        """任务终态 → finished；等本人决定 → asked。"""
        assert record.host_ref is not None
        task = await self._platform.work_tasks.store.get(who, record.host_ref)
        if task.status in TASK_TERMINAL:
            await self._emit(record, f"task:{task.status}:{task.state_version}", "finished")
        elif task.status == "waiting_user" and record.state == "running":
            await self._emit(record, f"task:waiting:{task.state_version}", "asked")

    async def _reconcile_session(self, record: DispatchRecord) -> None:
        """会话每结束一轮写一条 finished。"""
        assert record.host_ref is not None
        rounds = await self._platform.store.query_audit(record.tenant_id, AuditQuery(
            session_id=record.host_ref, kind="turn_text", limit=1000))
        for audit in rounds:
            await self._emit(record, f"{_ROUND_PREFIX}{audit.audit_id}", "finished")

    async def _emit(self, record: DispatchRecord, key: str, kind: str) -> None:
        """写事件；本进程已写过的键跳过（存储侧仍按键去重）。"""
        marker = (record.dispatch_id, key)
        if marker in self._seen:
            return
        await self._platform.store.wake_store.emit(ButlerEvent(
            event_id=new_id("bev"), tenant_id=record.tenant_id, user_id=record.user_id,
            dispatch_id=record.dispatch_id, key=key, kind=kind,  # type: ignore[arg-type]
            created_at=self._at()))
        self._seen.add(marker)

    async def handle(self, event: ButlerEvent) -> None:
        """处理一条事件：取或算决定 → 执行副作用 → 确认。"""
        store = self._platform.store.wake_store
        who = principal(event.tenant_id, event.user_id)
        owner = self._effects.owner(who)
        dispatch = await self._platform.dispatches.get(owner, event.dispatch_id)
        if dispatch.state != "delivered":
            decision = event.decision
            if decision is None:
                decision = (await store.decide(event.tenant_id, event.event_id,
                                               await self._decide(who, event, dispatch))).decision
            assert decision is not None
            await self._effects.apply(who, event, dispatch, decision)
            if decision.action == "deliver":
                self._timers.cancel(f"dispatch:{event.dispatch_id}")
        await store.ack(event.tenant_id, event.event_id, self._at())

    async def _decide(self, who: Principal, event: ButlerEvent, dispatch: DispatchRecord,
                      ) -> WakeDecision:
        """读宿主、确定性验收、（可选）管家复核，得出决定。"""
        if event.kind == "asked":
            return WakeDecision(action="await_approval")
        snapshot = await self._snapshot(who, event, dispatch)
        snapshot = replace(snapshot, result_text=await self._result_only(dispatch, snapshot.result_text))
        if snapshot.outcome == "suspended":
            return WakeDecision(action="await_approval", artisan_text=snapshot.result_text or None)
        result = inspect(snapshot)
        base = WakeDecision(action="deliver", passed=result.passed, checks=result.checks,
                            artisan_text=snapshot.result_text or None,
                            final_state=result.final_state, end_reason=result.end_reason)
        if self._reviewer is None:
            return base
        can_answer = snapshot.host == "session" and snapshot.outcome == "completed"
        return await self._reviewer.review(who, event, dispatch, base, can_answer=can_answer)

    async def _result_only(self, dispatch: DispatchRecord, text: str) -> str:
        """去掉匠人回复里已作为旁白讲过的话（R7）；读不到执行记录时如实保留原文并记日志。"""
        if not text:
            return text
        owner = TraceOwner(tenant_id=dispatch.tenant_id, user_id=dispatch.user_id)
        try:
            view = await self._platform.store.trace_store.get(owner, dispatch.trace_id)
        except Exception:
            logger.exception("派工 %s 读执行记录失败，结果保留原文", dispatch.dispatch_id)
            return text
        by_id = {s.span_id: s for s in view.spans}
        root = dispatch_span(dispatch.dispatch_id)

        def mine(span_id: str | None) -> bool:
            """这个 span 在本次派工之下。"""
            seen: set[str] = set()
            while span_id is not None and span_id not in seen:
                if span_id == root:
                    return True
                seen.add(span_id)
                span_id = by_id[span_id].parent_span_id if span_id in by_id else None
            return False

        said = [s.say for s in view.spans if s.say and mine(s.span_id)]
        return without_narration(text, said)

    async def _snapshot(self, who: Principal, event: ButlerEvent, dispatch: DispatchRecord,
                        ) -> HostSnapshot:
        """按事件键读宿主。"""
        assert dispatch.host_ref is not None
        if dispatch.host == "task":
            return await task_snapshot(self._platform, who, dispatch.host_ref)
        audit_id = int(event.key.removeprefix(_ROUND_PREFIX))
        found = await self._platform.store.query_audit(event.tenant_id, AuditQuery(
            session_id=dispatch.host_ref, kind="turn_text", after_id=audit_id - 1, limit=1))
        if not found or found[0].audit_id != audit_id:
            raise NotFoundError("匠人这一轮的记录不存在")
        record = found[0]
        return await session_round(self._platform, event.tenant_id, dispatch.host_ref,
                                   str(record.data.get("submission_id", "")),
                                   str(record.data.get("text", "")))
