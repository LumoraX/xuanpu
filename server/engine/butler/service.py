"""管家入口服务：``POST /v1/assistant/messages`` 背后的逻辑。

- 租户未开启管家（或未同时开启执行追踪）时，原样走既有分流，回执标 ``mode=route``。
- 开启时：先把员工原话写进主对话（失败也不丢输入），按 ``request_id`` 认领（同键同体回原回执，
  不同体 409），跑一轮管家，写回执与助手消息。派工只建记录并启动匠人，不等结果。
- 回执与一轮解耦（R6-A，ADR 0020）：一轮在平台后台跑，请求只等 ``reply_window_seconds``；
  没办完先回 ``state=running``，之后回复写进主对话。员工可以接着说下一句，各轮互不阻塞。
- 提出的草稿与分流产出的卡片同形，沿用既有的待确认与确认接口。
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from server.core.errors import PlatformError, RouteBusyError, RuntimeUnavailableError
from server.core.ids import new_id
from server.domains.work_tasks.models import TERMINAL as WORK_TASK_TERMINAL
from server.domains.work_tasks.models import InputFile  # noqa: TC001 —— Pydantic 运行时解析
from server.engine.assistant.service import RouteBody
from server.engine.butler.dispatcher import Dispatcher
from server.engine.butler.engine import ButlerEngines, TurnTimeout
from server.engine.butler.groups.rater import ButlerDivergenceRater
from server.engine.butler.groups.runner import GroupRunner
from server.engine.butler.groups.trace import TracedGroupStore
from server.engine.butler.pending import PendingService, owner_of
from server.engine.butler.review import Reviewer
from server.engine.butler.session_queue import (
    ActiveSessionSlot,
    MessageMode,  # noqa: TC001 —— Pydantic 运行时解析
    SessionQueueCoordinator,
)
from server.engine.butler.task_board import board_view
from server.engine.butler.tools import ButlerTools, has_draft
from server.engine.butler.turn import ButlerTurn, TurnRegistry
from server.engine.butler.wake_apply import WakeEffects
from server.engine.butler.wake_bus import MemoryWakeBus, PostgresWakeBus, WakeSignal
from server.engine.butler.wakeup import Wakeup

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from server.core.tenancy import Principal
    from server.domains.work_tasks.models import WorkTaskRecord
    from server.engine.assistant.reminders import RouteOutcome
    from server.engine.butler.proactive_briefing import ProactiveBriefingService
    from server.engine.butler.session_queue import MergePayload
    from server.engine.butler.topic_watch import TopicWatchStore
    from server.engine.butler.wake_bus import WakeBus
    from server.engine.runtime.models import ModelClientFactory
    from server.platform import Platform

logger = logging.getLogger(__name__)
HISTORY_MESSAGES = 6


class ClientLocation(BaseModel):
    """客户端可选附带的当前位置提示（仅作环境数据，不作为权限依据）。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    accuracy_meters: float | None = Field(default=None, ge=0.0, le=100_000.0)
    city: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=200)


class ButlerMessageBody(BaseModel):
    """员工对管家说的一句话。客户端不能指定租户、匠人或预算。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.:-]+$")
    text: str = Field(min_length=1, max_length=8000, pattern=r"\S")
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    input_files: list[InputFile] = Field(default_factory=list, max_length=8)
    location: ClientLocation | None = None
    mode: MessageMode = "default"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        """时区只允许系统支持的 IANA 标识。"""
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError("无效时区") from exc
        return value


class ButlerService:
    """管家：入口、引擎、派工的装配点。"""

    def __init__(self, platform: Platform, models: ModelClientFactory, *, data_dir: Path,
                 now: Callable[[], float] = time.time) -> None:
        """依赖由平台注入。"""
        self._platform = platform
        self._now = now
        self.turns = TurnRegistry()
        self.wake_bus: WakeBus = _build_wake_bus(platform)
        self.dispatcher = Dispatcher(platform, now=now, bus=self.wake_bus)
        self.pending = PendingService(platform, now=now, bus=self.wake_bus)
        self.group_store = TracedGroupStore(platform.store.group_store, platform.store.trace_store,
                                            lambda: platform.runtimes.defaults.execution_trace)
        self.groups = GroupRunner(platform, self.dispatcher, self.group_store, now=now)
        self.tools = ButlerTools(platform, self.dispatcher, self.turns, pending=self.pending,
                                 groups=self.groups, now=now)
        self.engines = ButlerEngines(platform, models, self.tools, self.turns, data_dir=data_dir)
        self.groups.use_rater(ButlerDivergenceRater(platform, self.engines))
        self.pending.bind_ruling(self.groups.rule)
        self.wakeup = Wakeup(platform, WakeEffects(platform, self.pending, now=now,
                                                   groups=self.groups),
                             Reviewer(platform, self.engines), now=now, groups=self.groups,
                             bus=self.wake_bus)
        platform.work_tasks.store.transition_listeners.append(self._on_work_task_transition)
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        # 进行中的轮次：键为 租户:员工:request_id；同标识重试直接回进行中回执，不再跑
        self._running: dict[str, _RunningTurn] = {}
        self.session_queue = SessionQueueCoordinator(
            platform, max_merges=lambda: platform.butler_config.max_interrupt_merges)
        from server.engine.butler.proactive_briefing import ProactiveBriefingService
        from server.engine.butler.topic_watch import TopicWatchStore

        self.briefings: ProactiveBriefingService = ProactiveBriefingService(platform, now=now)
        self.topic_watches: TopicWatchStore = TopicWatchStore(data_dir, collections=platform.plugins.collections)

    async def _on_work_task_transition(self, record: WorkTaskRecord) -> None:
        """工作任务进入终态或等待本人决定时，立即向唤醒总线发事件驱动信号。"""
        if not self.enabled(record.tenant_id):
            return
        if record.status in WORK_TASK_TERMINAL or record.status == "waiting_user":
            await self.wake_bus.publish(WakeSignal(
                tenant_id=record.tenant_id, kind="task", target_id=record.task_id))

    async def wait_turn_idle(self, trace_id: str, *, timeout: float = 5.0) -> None:
        """若本实例上发起该 trace 的管家轮次尚未收尾，先等它写完回复与 trace 再做事件快路径唤醒。"""
        active = [r.task for r in self._running.values() if r.trace_id == trace_id and not r.task.done()]
        if active:
            await asyncio.wait(active, timeout=timeout)

    def enabled(self, tenant: str) -> bool:
        """管家与执行追踪同时对该租户开启才走管家（派工预算与执行树依赖追踪）。"""
        p = self._platform
        return p.butler_config.allows(tenant) and p.runtimes.defaults.execution_trace.allows(tenant)

    async def message(self, who: Principal, body: ButlerMessageBody) -> dict[str, Any]:
        """处理一句话，返回回执。未开启的租户走既有分流。

        一轮在后台任务里跑，这里只等回复窗口：办完回完整回执（``state=done``），
        没办完回 ``state=running`` 与 ``trace_id``，这一轮继续，结束后回复写进主对话。
        窗口内失败照旧抛给调用方（503，原话已保留）。
        """
        if who.tenant_id is None or not self.enabled(who.tenant_id) or not who.user_id:
            return await self._route(who, body)
        await self._platform.workspace.require_employee(who)
        user_key = f"{who.tenant_id}:{who.user_id}"
        merge: MergePayload | None = None
        if body.mode == "interrupt_merge":
            merge = await self.session_queue.interrupt_and_prepare_merge(
                user_key, body.request_id, body.text)
        use_queue = (
            merge is None
            and self.session_queue.active_slot(user_key) is not None
            and (body.mode in ("queue", "interrupt_merge")
                 or self._platform.butler_config.serialize_user_turns)
            and f"{user_key}:{body.request_id}" not in self._running
        )
        if use_queue:
            await self._platform.workspace.append_message(
                who, "user", body.text, body.request_id + ":user",
                attachments=[f.item_id for f in body.input_files])
            _, position = await self.session_queue.enqueue(
                user_key, body.request_id,
                lambda: self._start(who, body, detached=True).task)
            return {
                "mode": "butler", "state": "queued", "request_id": body.request_id,
                "queue_position": position,
            }
        running = self._start(who, body, merge=merge)
        window = self._platform.butler_config.reply_window_seconds
        try:
            done, _ = await asyncio.wait({running.task}, timeout=window)
        except asyncio.CancelledError:
            # 员工断开（关页面）：这一轮照常在后台跑完，结果进主对话
            running.detached = True
            raise
        if running.task in done:
            return running.task.result()
        running.detached = True
        return {"mode": "butler", "state": "running", "request_id": body.request_id,
                "trace_id": running.trace_id}

    async def open_stream(self, who: Principal, body: ButlerMessageBody,
                          ) -> dict[str, Any] | _RunningTurn:
        """流式回执的开头（R7）：未开启管家的租户直接给分流回执；否则启动这一轮并返回它。

        身份校验在这里做完，出错照常以 HTTP 状态返回；之后的事件由 ``stream_events`` 推送。
        """
        if who.tenant_id is None or not self.enabled(who.tenant_id) or not who.user_id:
            return await self._route(who, body)
        await self._platform.workspace.require_employee(who)
        user_key = f"{who.tenant_id}:{who.user_id}"
        merge: MergePayload | None = None
        if body.mode == "interrupt_merge":
            merge = await self.session_queue.interrupt_and_prepare_merge(
                user_key, body.request_id, body.text)
        elif (
            self._platform.butler_config.serialize_user_turns
            and self.session_queue.active_slot(user_key) is not None
            and f"{who.tenant_id}:{who.user_id}:{body.request_id}" not in self._running
        ):
            # SSE 入口与 JSON 入口保持一致：同员工前轮进行中时先落盘原文并按 FIFO 入队
            await self._platform.workspace.append_message(
                who, "user", body.text, body.request_id + ":user",
                attachments=[f.item_id for f in body.input_files])
            _, position = await self.session_queue.enqueue(
                user_key, body.request_id,
                lambda: self._start(who, body, detached=True).task,
            )
            return {
                "mode": "butler",
                "state": "queued",
                "request_id": body.request_id,
                "queue_position": position,
            }
        return self._start(who, body, merge=merge)

    async def stream_events(self, body: ButlerMessageBody, running: _RunningTurn,
                            ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """先推 ``started``（带预先确定的 trace 标识），再推回复增量，最后推回执或错误。

        订阅者断开时这一轮照常在后台跑完，结果进主对话（与回复窗口过后一致）。
        """
        yield "started", {"mode": "butler", "request_id": body.request_id, "trace_id": running.trace_id}
        sent = 0
        sent_steps = 0
        sent_focus = 0
        sent_plans = 0
        try:
            while True:
                waiter = asyncio.ensure_future(running.changed.wait())
                try:
                    await asyncio.wait({running.task, waiter}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    waiter.cancel()
                running.changed.clear()
                while sent_plans < len(running.plans):
                    yield "plan", {"text": running.plans[sent_plans]}
                    sent_plans += 1
                while sent_steps < len(running.steps):
                    yield "step", {"text": running.steps[sent_steps]}
                    sent_steps += 1
                while sent_focus < len(running.focus_updates):
                    yield "focus", running.focus_updates[sent_focus]
                    sent_focus += 1
                if len(running.deltas) > sent:
                    yield "delta", {"text": "".join(running.deltas[sent:])}
                    sent = len(running.deltas)
                if running.superseded_by is not None:
                    yield "superseded", {
                        "request_id": body.request_id,
                        "superseded_by": running.superseded_by,
                        "merged_request_ids": list(running.superseded_merged_ids),
                    }
                    return
                if running.task.done():
                    break
        except BaseException:
            running.detached = True
            raise
        if running.superseded_by is not None:
            yield "superseded", {
                "request_id": body.request_id,
                "superseded_by": running.superseded_by,
                "merged_request_ids": list(running.superseded_merged_ids),
            }
            return
        yield _final(running.task)

    def _start(self, who: Principal, body: ButlerMessageBody, *,
               detached: bool = False, merge: MergePayload | None = None) -> _RunningTurn:
        """登记并启动一轮后台任务；同一句话已在本进程里跑则复用。"""
        user_key = f"{who.tenant_id}:{who.user_id}"
        key = f"{user_key}:{body.request_id}"
        running = self._running.get(key)
        if running is None:
            running = _RunningTurn(trace_id=new_id("trc"), detached=detached)
            running.task = asyncio.create_task(self._serve(who, body, running, merge=merge))
            self._running[key] = running
            running.task.add_done_callback(functools.partial(self._finished, key))
            self.session_queue.register_active(ActiveSessionSlot(
                user_key=user_key,
                request_id=body.request_id,
                text=merge.merged_text if merge is not None else body.text,
                who=who,
                task=running.task,
                on_superseded=running.mark_superseded,
                merged_request_ids=merge.merged_request_ids if merge is not None else (),
                merge_count=merge.merge_count if merge is not None else 0,
            ))
        return running

    async def resume_stale(self, tenant: str) -> int:
        """持有者已不在的轮次（进程中途退出、租约过期）：按同一请求标识重跑一次（R6-D）。

        派工按派工请求标识幂等，重跑不会重复派出。已重跑过仍中断的不再重跑：主对话写明
        没处理完、原话保留，并释放认领。返回重新启动的轮次数。
        """
        from server.engine.butler.wakeup import principal

        started = 0
        for user, key, claim in await self._platform.workspace.stale_routes(tenant, "butler:"):
            request_id = key.removeprefix("route:butler:")
            raw = claim.get("butler_input")
            if raw is None or f"{tenant}:{user}:{request_id}" in self._running:
                continue
            who = principal(tenant, user)
            body = ButlerMessageBody.model_validate({**raw, "request_id": request_id})
            if int(claim.get("resumes", 0)) >= _MAX_RESUMES:
                await self._report_failure(who, body)
                await self._platform.workspace.release_route(who, "butler:" + request_id,
                                                             str(claim["token"]))
                continue
            self._start(who, body, detached=True)
            started += 1
        return started

    def _finished(self, key: str, task: asyncio.Task[dict[str, Any]]) -> None:
        """后台轮次结束：移出登记；没人等待时取走异常（已在 ``_serve`` 里记日志并写进主对话）。"""
        self._running.pop(key, None)
        if not task.cancelled() and task.exception() is not None:
            logger.info("管家后台轮次以错误结束：%s", type(task.exception()).__name__)

    async def _serve(self, who: Principal, body: ButlerMessageBody,
                     running: _RunningTurn, *, merge: MergePayload | None = None) -> dict[str, Any]:
        """后台的一轮：认领、续租、处理、完成。回复窗口过后失败的，在主对话写明并保留原话。"""
        p = self._platform
        lock = self._locks.setdefault(f"{who.tenant_id}:{who.user_id}:{body.request_id}",
                                      asyncio.Lock())
        async with lock:
            # 先保存原话：之后任何一步失败，员工的输入都还在主对话里，可以按同一标识重试
            await p.workspace.append_message(
                who, "user", body.text, body.request_id + ":user",
                attachments=[f.item_id for f in body.input_files])
            claim_body = body.model_dump(exclude={"request_id", "mode"}, exclude_none=True)
            try:
                claim = await p.workspace.claim_route(
                    who,
                    "butler:" + body.request_id,
                    claim_body,
                )
            except RouteBusyError:
                # 同一句话正由另一实例处理（租约未过期）：照常回「进行中」，结果由那边写进主对话
                return {"mode": "butler", "state": "running", "request_id": body.request_id,
                        "trace_id": None, "elsewhere": True}
            if claim["state"] == "complete":
                return dict(claim["outcome"])
            # 记下原始输入：进程中途退出后，唤醒循环据此按同一标识重跑（R6-D）
            resumes = int(claim.get("resumes", 0)) + (1 if "butler_input" in claim else 0)
            await p.workspace.annotate_route(
                who, "butler:" + body.request_id, claim["token"],
                {"butler_input": body.model_dump(mode="json", exclude={"request_id", "mode"}, exclude_none=True),
                 "resumes": resumes})
            renewer = asyncio.create_task(self._renew(who, body.request_id, claim["token"]))
            try:
                receipt = await self._handle(
                    who, body, running.trace_id, running.hear, running.push_step,
                    on_focus=running.push_focus, on_plan=running.push_plan, merge=merge,
                )
                await p.workspace.complete_route(who, "butler:" + body.request_id,
                                                 claim["token"], receipt)
                return receipt
            except asyncio.CancelledError:
                raise
            except Exception:
                if running.detached:
                    await self._report_failure(who, body)
                raise
            finally:
                renewer.cancel()
                await asyncio.gather(renewer, return_exceptions=True)
                await p.workspace.release_route(who, "butler:" + body.request_id, claim["token"])

    async def _renew(self, who: Principal, request_id: str, token: str) -> None:
        """一轮进行中每 20 秒续一次 60 秒租约；进程退出后租约在一分钟内过期，可按同一标识重跑。"""
        while True:
            await asyncio.sleep(_RENEW_SECONDS)
            await self._platform.workspace.renew_route(who, "butler:" + request_id, token,
                                                       _LEASE_SECONDS)

    async def _report_failure(self, who: Principal, body: ButlerMessageBody) -> None:
        """员工已经离开等待（拿到了进行中回执）：失败写进主对话，说明原话已保留、可以重说。"""
        try:
            await self._platform.workspace.append_message(
                who, "assistant", "刚才这件事没有处理完：管家暂时无法处理。你的原话已保留，可以稍后再说一次。",
                body.request_id + ":assistant", metadata={"butler": {"failed": True}})
        except Exception:
            logger.exception("管家失败说明写入主对话失败")

    async def _route(self, who: Principal, body: ButlerMessageBody) -> dict[str, Any]:
        """降级：既有分流，回执与 ``/v1/assistant/route`` 同形。"""
        from server.api.assistant import route_view

        outcome = await self._platform.assistant.route(who, RouteBody(
            text=body.text, request_id=body.request_id, timezone=body.timezone,
            attachments=[f.item_id for f in body.input_files], input_files=body.input_files))
        return {"mode": "route", **route_view(outcome)}

    async def _handle(self, who: Principal, body: ButlerMessageBody, trace_id: str,
                      on_text: Callable[[str], None] | None = None,
                      on_step: Callable[[str], None] | None = None, *,
                      on_focus: Callable[[dict[str, Any]], None] | None = None,
                      on_plan: Callable[[str], None] | None = None,
                      merge: MergePayload | None = None) -> dict[str, Any]:
        """跑一轮管家并写助手消息；失败统一成可重试的错误，输入已保留。"""
        p, tenant = self._platform, who.require_tenant()
        await p.quota.check_new_turn(tenant)
        effective_text = merge.merged_text if merge is not None else body.text
        turn = ButlerTurn(
            who=who, request_id=body.request_id, trace_id=trace_id,
            timezone=body.timezone, materials=tuple(body.input_files),
            user_text=effective_text, on_text=on_text, on_step=on_step,
            on_focus=on_focus, on_plan=on_plan,
            merged_request_ids=merge.merged_request_ids if merge is not None else (),
            prior_in_flight=merge.prior_in_flight if merge is not None else None,
        )
        self.session_queue.bind_turn(f"{who.tenant_id}:{who.user_id}", body.request_id, turn)
        await p.sessions.admit_routing(tenant)
        try:
            from server.engine.butler.multimodal import collect_inline_images

            images = await collect_inline_images(p, who, body.input_files)
            payload = await self._payload(who, body, merge=merge)
            reply = (
                await self.engines.run(turn, payload, attachments=images)
                if images
                else await self.engines.run(turn, payload)
            )
        except TurnTimeout as exc:
            reply = _timed_out(turn, exc)
        except PlatformError:
            raise
        except Exception:
            logger.exception("管家处理失败")
            raise RuntimeUnavailableError("管家暂时无法处理，输入已保留，请稍后重试") from None
        finally:
            p.sessions.release_routing(tenant)
        outcome = self._outcome(turn, reply)
        saved = outcome.saved()
        if turn.executed_actions:
            saved["executed_actions"] = list(turn.executed_actions)
        butler_meta: dict[str, Any] = {"trace_id": turn.trace_id}
        if turn.merged_request_ids:
            butler_meta["merged_request_ids"] = list(turn.merged_request_ids)
        await p.workspace.append_message(
            who, "assistant", outcome.message or "已办妥。", body.request_id + ":assistant",
            metadata={"route": saved, "butler": butler_meta})
        result: dict[str, Any] = {
            "mode": "butler", "state": "done", "request_id": body.request_id,
            "trace_id": turn.trace_id,
            **saved, "dispatches": [_dispatch_view(r) for r in turn.dispatches],
            "groups": list(turn.groups),
            "answered_pending": list(turn.answered),
        }
        if turn.merged_request_ids:
            result["merged_request_ids"] = list(turn.merged_request_ids)
        return result

    def _outcome(self, turn: ButlerTurn, reply: str) -> RouteOutcome:
        """回复文本为准；有草稿时带上卡片并以本请求作为待确认标识。"""
        from server.engine.assistant.reminders import RouteOutcome

        if turn.proposal is None:
            return RouteOutcome(message=reply or None)
        return replace(turn.proposal, message=reply or turn.proposal.message,
                       proposal_id=turn.request_id if has_draft(turn.proposal) else None)

    async def _payload(
        self, who: Principal, body: ButlerMessageBody, *, merge: MergePayload | None = None,
    ) -> str:
        """本轮输入：最近对话、活跃任务板工作记忆、当前时间与原话，全部作为数据交给管家。"""
        recent = await self._platform.workspace.messages(who, limit=HISTORY_MESSAGES + 1,
                                                         latest=True, private_only=True)
        history = [{"role": m["role"], "text": m["text"][-2000:]} for m in recent
                   if m["request_id"] != body.request_id + ":user"][-HISTORY_MESSAGES:]
        questions = [{"pending_id": i.pending_id, "question": i.question}
                     for i in await self.pending.list(who) if i.kind == "question"]
        settings = await self._platform.workspace.settings(who)
        raw_skills = await self._platform.workspace.list(who, kind="skill")
        skills = [
            {
                "title": str(s["data"].get("title") or ""),
                "instructions": str(s["data"].get("instructions") or "")[:1000],
            }
            for s in raw_skills
            if s["data"].get("enabled", True)
        ][:8]
        owner = owner_of(who)
        active_boards = await self._platform.store.task_board_store.list_active(owner, limit=5)
        recent_dispatches = await self._platform.dispatches.list_recent(owner, limit=5)
        working_context: dict[str, Any] = {
            "task_boards": [board_view(b) for b in active_boards],
            "active_dispatches": [
                _dispatch_view(d) for d in recent_dispatches if d.active
            ],
        }
        if merge is not None and merge.prior_in_flight is not None:
            working_context["prior_in_flight"] = merge.prior_in_flight
        payload: dict[str, Any] = {
            "now": datetime.fromtimestamp(self._now(), ZoneInfo(body.timezone)).isoformat(),
            "timezone": body.timezone, "turn_seconds": self._platform.butler_config.turn_seconds,
            "personal": {
                "assistant_name": str(settings.get("assistant_name") or "玄圃助手"),
                "user_title": str(settings.get("user_title") or ""),
                "style": str(settings.get("style") or "简洁、清晰"),
                "auto_execute_personal": bool(settings.get("auto_execute_personal", True)),
                "skills": skills,
            },
            "working_context": working_context,
            "history": history, "pending_questions": questions,
            "materials": [f.model_dump() for f in body.input_files],
            "text": merge.merged_text if merge is not None else body.text,
        }
        if body.location is not None:
            payload["location"] = body.location.model_dump(exclude_none=True)
        return json.dumps(payload, ensure_ascii=False)

    def start(self) -> None:
        """仅在存在启用管家的租户时启动唤醒循环（避免空占 PG LISTEN 连接）。"""
        cfg = self._platform.butler_config
        if not (cfg.enabled and cfg.tenants):
            return
        self.wakeup.start()

    async def close(self) -> None:
        """先停后台轮次、队列与唤醒，再关引擎池。被取消的一轮不写半截回复，认领随之释放。"""
        await self.session_queue.close()
        running = [r.task for r in self._running.values()]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await self.wakeup.close()
        await self.dispatcher.close()
        await self.pending.close()
        await self.engines.close()



def _timed_out(turn: ButlerTurn, exc: TurnTimeout) -> str:
    """一轮到时限的收尾说明：不是失败；已交出去的工作照常进行，结束后会回报。"""
    spent = (f"约 {round(exc.elapsed / 60)} 分钟" if exc.elapsed >= 90
             else f"{max(1, round(exc.elapsed))} 秒")
    text = f"这件事我处理了 {spent}，这一轮已到时限先停下。"
    if turn.dispatches or turn.groups:
        names = "、".join(dict.fromkeys(d.artisan.package for d in turn.dispatches)) or "编组"
        text += f"已交出去的工作（{names}）仍在进行，完成后我会告诉你结果。"
    else:
        text += "如果还需要继续，可以再跟我说一次，或者让我拆成几件交给别人做。"
    return text


def _final(task: asyncio.Task[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """一轮结束时的最后一帧：回执，或错误码与文案（平台错误如实给出，其余统一为内部错误）。"""
    if task.cancelled():
        return "error", {"code": "runtime_unavailable", "message": "这一轮被中止，输入已保留，请稍后重试"}
    error = task.exception()
    if error is None:
        return "receipt", task.result()
    if isinstance(error, PlatformError):
        return "error", {"code": error.code, "message": str(error)}
    logger.error("管家一轮以意外错误结束：%s", type(error).__name__)
    return "error", {"code": "internal_error", "message": "管家处理出错，输入已保留，请稍后重试"}


def _build_wake_bus(platform: Platform) -> WakeBus:
    """管家已启用且使用 PostgreSQL 控制面时走 LISTEN/NOTIFY，其余模式用进程内合并总线。"""
    cfg = platform.butler_config
    pool = getattr(platform.store, "_pool", None)
    if cfg.enabled and cfg.tenants and pool is not None:
        return PostgresWakeBus(pool)
    return MemoryWakeBus()


def _dispatch_view(record: Any) -> dict[str, Any]:
    """回执里的派工：只给标识、匠人与状态。"""
    return {"dispatch_id": record.dispatch_id, "package": record.artisan.package,
            "entry": record.artisan.entry, "state": record.state, "host": record.host}


_RENEW_SECONDS = 20.0
_MAX_RESUMES = 1
_LEASE_SECONDS = 60.0


@dataclass
class _RunningTurn:
    """进行中的一轮：后台任务、预先确定的 trace 标识、员工是否已经不再等待。"""

    trace_id: str
    task: asyncio.Task[dict[str, Any]] = field(init=False)
    detached: bool = False
    # 管家回复的文本增量与过程步骤（R7 流式回执）：按到达顺序累积，多个订阅者各自记读到哪里
    deltas: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    plans: list[str] = field(default_factory=list)
    focus_updates: list[dict[str, Any]] = field(default_factory=list)
    superseded_by: str | None = None
    superseded_merged_ids: tuple[str, ...] = ()
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def hear(self, delta: str) -> None:
        """记下一段回复文本并唤醒订阅者。"""
        self.deltas.append(delta)
        self.changed.set()

    def push_step(self, step: str) -> None:
        """记下一条执行过程步骤并唤醒流式订阅者。"""
        if not self.steps or self.steps[-1] != step:
            self.steps.append(step)
            self.changed.set()

    def push_plan(self, plan_text: str) -> None:
        """记下工具前的一句话规划预告并唤醒流式订阅者。"""
        self.plans.append(plan_text)
        self.changed.set()

    def push_focus(self, snapshot: dict[str, Any]) -> None:
        """记下任务板实时焦点更新并唤醒流式订阅者。"""
        self.focus_updates.append(snapshot)
        self.changed.set()

    def mark_superseded(self, new_request_id: str, merged_ids: tuple[str, ...]) -> None:
        """标记本轮已被强制插入合并取代，通知流式订阅者平滑切换。"""
        self.superseded_by = new_request_id
        self.superseded_merged_ids = merged_ids
        self.changed.set()
