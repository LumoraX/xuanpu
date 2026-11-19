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

from xuanpu_server.butler.inspector import inspect, session_round, task_snapshot
from xuanpu_server.butler.trace_link import dispatch_span
from xuanpu_server.butler.wake_models import ButlerEvent, WakeDecision
from xuanpu_server.controlplane.models import AuditQuery
from xuanpu_server.errors import NotFoundError
from xuanpu_server.ids import new_id
from xuanpu_server.tenancy import Principal
from xuanpu_server.tracing.models import TraceOwner
from xuanpu_server.tracing.narration import without_narration
from xuanpu_server.work_tasks.models import TERMINAL as TASK_TERMINAL

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.butler.groups.runner import GroupRunner
    from xuanpu_server.butler.inspector import HostSnapshot
    from xuanpu_server.butler.models import DispatchRecord
    from xuanpu_server.butler.review import Reviewer
    from xuanpu_server.butler.wake_apply import WakeEffects
    from xuanpu_server.platform import Platform

logger = logging.getLogger(__name__)
_ROUND_PREFIX = "round:"


def principal(tenant_id: str, user_id: str) -> Principal:
    """派工主人的员工身份（后台处理以本人身份读写本人数据）。"""
    return Principal(role="tenant_member", tenant_id=tenant_id, user_id=user_id, key_id=None)


class Wakeup:
    """对账、处理与后台循环。"""

    def __init__(self, platform: Platform, effects: WakeEffects, reviewer: Reviewer | None, *,
                 now: Callable[[], float], groups: GroupRunner | None = None) -> None:
        """依赖由平台注入；``reviewer`` 为 None 时只做确定性验收；``groups`` 为 None 时不推进编组。"""
        self._groups = groups
        self._platform = platform
        self._effects = effects
        self._reviewer = reviewer
        self._now = now
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._seen: set[tuple[str, str]] = set()

    def _at(self) -> datetime:
        """当前时间（UTC）。"""
        return datetime.fromtimestamp(self._now(), UTC)

    def start(self) -> None:
        """启动后台循环。"""
        self._task = asyncio.ensure_future(self._loop())

    async def close(self) -> None:
        """停后台循环。"""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        """按配置间隔对账与处理；单轮失败记日志，下一轮重试。"""
        while True:
            await asyncio.sleep(self._platform.butler_config.wake_interval_seconds)
            try:
                await self.run_once()
            except Exception:
                logger.exception("管家唤醒处理失败，下一轮重试")

    async def run_once(self) -> int:
        """对每个开启管家的租户对账一次、处理全部未确认事件；返回处理条数。"""
        handled = 0
        async with self._lock:
            for tenant in self._platform.butler_config.tenants:
                if not self._platform.butler.enabled(tenant):
                    continue
                # 进程中途退出留下的轮次：按同一请求标识重跑一次（R6-D）
                await self._platform.butler.resume_stale(tenant)
                await self.reconcile(tenant)
                for event in await self._platform.store.wake_store.unacked(tenant):
                    try:
                        await self.handle(event)
                    except Exception:
                        # 单条失败不挡住后面的事件；它保持未确认，下一轮重试
                        logger.exception("处理唤醒事件 %s 失败，下一轮重试", event.event_id)
                        continue
                    handled += 1
                if self._groups is not None:
                    # 成员结果已随事件登记；接着推进编组（启动下一成员、收口汇总）
                    await self._groups.advance_tenant(tenant)
        return handled

    async def reconcile(self, tenant: str) -> None:
        """扫进行中的派工，把宿主上的新事实写成事件。"""
        for record in await self._platform.dispatches.active(tenant):
            if record.state not in ("running", "asking") or record.host_ref is None:
                continue
            who = principal(record.tenant_id, record.user_id)
            try:
                if record.host == "task":
                    await self._reconcile_task(who, record)
                else:
                    await self._reconcile_session(record)
            except Exception:
                # 单个派工读不到宿主（例如员工已停用）不挡住其他派工；下一轮重试
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
