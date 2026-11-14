"""单匠人派工：解析匠人、核对预算、建派工记录、启动承载执行，然后立即返回。

不等匠人结束：结果回到对话（唤醒、验收、交付）在 R5-C2。承载方式由匠人解析决定：
持久任务（``accept_capability``）或既有会话（``sessions.create`` + ``send_message``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from xuanpu_server.butler.budget import TraceUsage, check_dispatch
from xuanpu_server.butler.models import INSTRUCTION_MAX, DispatchRecord, dispatch_fingerprint
from xuanpu_server.errors import InvalidRequestError, NotFoundError, PlatformError
from xuanpu_server.ids import new_id
from xuanpu_server.tracing.models import TERMINAL_STATES, TraceOwner
from xuanpu_server.work_tasks.models import CapabilityReference, CapabilityTaskInput

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.butler.models import Artisan
    from xuanpu_server.butler.turn import ButlerTurn
    from xuanpu_server.platform import Platform
    from xuanpu_server.runtime.sessions import Turn
    from xuanpu_server.work_tasks.models import InputFile

logger = logging.getLogger(__name__)


class Dispatcher:
    """管家派工。启动的会话轮次在后台读完，平台关闭时取消。"""

    def __init__(self, platform: Platform, *, now: Callable[[], float]) -> None:
        """依赖由平台注入。"""
        self._platform = platform
        self._now = now
        self._drains: set[asyncio.Task[None]] = set()

    def _at(self) -> datetime:
        """当前时间（UTC）。"""
        return datetime.fromtimestamp(self._now(), UTC)

    async def dispatch(
        self, turn: ButlerTurn, *, package: str, entry: str, instruction: str,
        materials: tuple[InputFile, ...],
    ) -> DispatchRecord:
        """派给一个匠人；同一输入重试时回放原派工，不重复启动。

        Raises:
            InvalidRequestError: 说明为空或超长、材料不是员工本次提供的、会话承载不支持材料。
            NotFoundError: 匠人对本人不可用。
            ButlerBudgetError: 预算超限（不建记录）。
        """
        text = instruction.strip()
        if not text or len(text) > INSTRUCTION_MAX:
            raise InvalidRequestError(f"任务说明须为 1–{INSTRUCTION_MAX} 个字符")
        allowed = {(m.item_id, m.revision) for m in turn.materials}
        if any((m.item_id, m.revision) not in allowed for m in materials):
            raise InvalidRequestError("派工材料只能是员工本次明确提供的文件")
        artisan = await self._platform.artisans.resolve(turn.who, package, entry)
        if artisan.host == "session" and materials:
            raise InvalidRequestError("这位匠人暂不支持附带文件材料")
        record = await self._create(turn, artisan, text, materials)
        turn.dispatches.append(record)
        if record.state != "accepted":
            return record
        return await self._start(turn, record, artisan, materials)

    async def _create(
        self, turn: ButlerTurn, artisan: Artisan, text: str, materials: tuple[InputFile, ...],
    ) -> DispatchRecord:
        """核对预算后建记录；存储在同一临界区里再核一次容量。"""
        config = self._platform.butler_config
        store = self._platform.store.dispatch_store
        owner = turn.owner()
        existing = await store.list_for_trace(owner, turn.trace_id)
        usage = TraceUsage(artisans=len(existing), tokens=await self._finished_tokens(owner, turn))
        refs = tuple(f"{m.item_id}@{m.revision}" for m in materials)
        request_id = turn.dispatch_request_id()
        replayed = next((r for r in existing if r.request_id == request_id), None)
        if replayed is None:
            check_dispatch(config, usage, trace_active=any(r.active for r in existing),
                           active_traces=await store.active_traces(owner.tenant_id))
        if not turn.root_seen.is_set():
            await asyncio.wait_for(turn.root_seen.wait(), timeout=10)
        now = self._at()
        record = DispatchRecord(
            dispatch_id=new_id("dsp"), tenant_id=owner.tenant_id, user_id=owner.user_id,
            request_id=request_id, trace_id=turn.trace_id, parent_span_id=turn.root_span,
            artisan=artisan.ref, host=artisan.host, instruction=text, materials=refs,
            input_fingerprint=dispatch_fingerprint(trace_id=turn.trace_id, artisan=artisan.ref,
                                                   instruction=text, materials=refs),
            created_at=now, updated_at=now,
        )
        created, _ = await store.create(owner, record,
                                        max_per_trace=config.max_artisans_per_trace,
                                        max_active_traces=config.max_active_traces_per_tenant)
        return created

    async def _finished_tokens(self, owner: TraceOwner, turn: ButlerTurn) -> int | None:
        """这条 trace 已结束的执行的 token 合计；进行中的尝试结束后才计入。

        trace 还没写出（采集按批写入）时没有已结束的执行，记 0；已结束的执行用量未知时
        返回 None，预算据此不放行。
        """
        try:
            view = await self._platform.store.trace_store.get(owner, turn.trace_id)
        except NotFoundError:
            return 0
        total = 0
        for span in view.spans:
            if span.state not in TERMINAL_STATES or not span.attempts:
                continue
            if span.input_tokens is None or span.output_tokens is None:
                return None
            total += span.input_tokens + span.output_tokens
        return total

    async def _start(
        self, turn: ButlerTurn, record: DispatchRecord, artisan: Artisan,
        materials: tuple[InputFile, ...],
    ) -> DispatchRecord:
        """启动承载执行并把派工推进到 running；启动失败记 failed 后原样上抛。"""
        store = self._platform.store.dispatch_store
        try:
            host_ref = (await self._start_task(turn, record, materials) if artisan.host == "task"
                        else await self._start_session(turn, record))
        except PlatformError:
            failed = await store.update(turn.owner(), record.dispatch_id,
                                        expected_version=record.version, to="failed",
                                        at=self._at(), end_reason="start_failed")
            turn.dispatches[-1] = failed
            raise
        running = await store.update(turn.owner(), record.dispatch_id,
                                     expected_version=record.version, to="running",
                                     at=self._at(), host_ref=host_ref)
        turn.dispatches[-1] = running
        return running

    async def _start_task(
        self, turn: ButlerTurn, record: DispatchRecord, materials: tuple[InputFile, ...],
    ) -> str:
        """持久任务承载：与 from-capability 同一受理口径。"""
        task = await self._platform.work_tasks.accept_capability(turn.who, CapabilityTaskInput(
            request_id="butler:" + record.dispatch_id, text=record.instruction,
            timezone=turn.timezone, input_files=materials,
            capability=CapabilityReference(package=record.artisan.package,
                                           entry=record.artisan.entry)))
        return task.task_id

    async def _start_session(self, turn: ButlerTurn, record: DispatchRecord) -> str:
        """会话承载：钉住派工时的版本建会话，发出任务说明；这一轮在后台读完。"""
        sessions = self._platform.sessions
        session = await sessions.create(
            turn.who, package=record.artisan.package, version=record.artisan.version,
            entry=record.artisan.entry, request_id="butler:" + record.dispatch_id)
        started = await sessions.send_message(turn.who, session.session_id, record.instruction)
        self._drain(started)
        return session.session_id

    def _drain(self, started: Turn) -> None:
        """读完这一轮的事件流，免得缓冲占满；内容已由事件枢纽进审计与追踪。"""
        async def consume() -> None:
            """逐条丢弃。"""
            async for _ in started.stream.events():
                pass

        task = asyncio.ensure_future(consume())
        self._drains.add(task)
        task.add_done_callback(self._drains.discard)

    async def close(self) -> None:
        """取消仍在读的后台轮次。"""
        tasks = tuple(self._drains)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

