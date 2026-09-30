"""引擎事件的分发：一个引擎一条全量订阅，同时喂给审计与各轮对话的事件流。

为什么不按 submission 各自订阅内核：订阅要等拿到 submission 标识之后才能建立，而内核在
提交之后随时可能开始发事件，先发出的那几条就丢了。这里在引擎创建时就建立全量订阅，
提交返回后**同步**登记该轮对话的接收队列——提交与登记之间没有让出点，内核来不及发事件。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from typing import TYPE_CHECKING, Any

from xuanpu_server.audit import SENSITIVE_KINDS, STREAMING_KINDS, sanitize
from xuanpu_server.controlplane.models import AuditRecord
from xuanpu_server.quota import usage_day

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import taifeng

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.controlplane.store import ControlPlaneStore

logger = logging.getLogger(__name__)

TERMINAL_KINDS: frozenset[str] = frozenset({"turn_completed", "turn_failed", "turn_suspended"})

_FLUSH_SIZE = 64
_FLUSH_INTERVAL_SECONDS = 0.5
# 单轮对话事件流在消费方读取之前最多缓冲多少条；超出即判定消费方已失联
_STREAM_BUFFER = 8192


class SubmissionIndex:
    """submission / 线程标识 → 会话标识。

    闸门在工具上下文里拿不到会话标识，只拿得到 submission 标识与线程标识；要把过闸记录
    归到会话名下，得靠这张表。两种标识都登记：审批通过后内核重跑工具的那条路径不带
    submission 标识，只能按线程归属。

    表有上限，超出时淘汰最早登记的：一轮对话结束很久之后不会再有工具调用找它。
    """

    def __init__(self, capacity: int = 100_000) -> None:
        """``capacity`` 是每种标识最多记多少条。"""
        self._capacity = capacity
        self._by_submission: dict[str, str] = {}
        self._by_thread: dict[str, str] = {}

    def _put(self, table: dict[str, str], key: str, session_id: str) -> None:
        """登记一条，超出上限时淘汰最早的。"""
        table.pop(key, None)
        table[key] = session_id
        while len(table) > self._capacity:
            # 字典保持插入顺序，第一个键就是最早登记的
            del table[next(iter(table))]

    def register(self, submission_id: str, session_id: str) -> None:
        """登记一轮对话属于哪个会话。"""
        self._put(self._by_submission, submission_id, session_id)

    def register_thread(self, thread_id: str, session_id: str) -> None:
        """登记一个线程属于哪个会话。"""
        self._put(self._by_thread, thread_id, session_id)

    def session_of(self, submission_id: object, thread_id: object = None) -> str | None:
        """查属于哪个会话：先按 submission，再按线程；都查不到返回 None。"""
        if isinstance(submission_id, str) and submission_id in self._by_submission:
            return self._by_submission[submission_id]
        if isinstance(thread_id, str):
            return self._by_thread.get(thread_id)
        return None


def is_terminal(event: taifeng.EventMsg) -> bool:
    """这条事件是否结束了它所属的那一轮对话。

    子 skill 的完成 / 失败事件也带着同一个 submission 标识，但它们发生在根 turn 的执行
    窗口之内，不算结束；挂起事件没有这层区分，出现即结束。
    """
    kind = event.msg.kind
    if kind == "turn_suspended":
        return True
    return kind in TERMINAL_KINDS and bool(event.msg.data.get("is_root", False))


class TurnStream:
    """一轮对话的事件流。"""

    def __init__(self, submission_id: str) -> None:
        """绑定一个 submission。"""
        self.submission_id = submission_id
        self._queue: asyncio.Queue[taifeng.EventMsg | None] = asyncio.Queue(_STREAM_BUFFER)
        self._closed = False
        self.overflowed = False

    def push(self, event: taifeng.EventMsg) -> None:
        """放入一条事件。缓冲满了就放弃这条流：不能因为一个不读的消费方拖住引擎。"""
        if self._closed:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.overflowed = True
            self._closed = True

    def close(self) -> None:
        """结束这条流。"""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)

    async def events(self) -> AsyncIterator[taifeng.EventMsg]:
        """逐条取事件，直到这一轮对话结束。"""
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event
            if is_terminal(event):
                return


class EngineEventHub:
    """一个引擎的事件枢纽。"""

    def __init__(
        self,
        engine: taifeng.AgentEngine,
        *,
        tenant_id: str,
        session_id: str,
        audit: AuditLog,
        store: ControlPlaneStore,
        capture_requests: bool = False,
    ) -> None:
        """
        Args:
            engine: 要订阅的引擎。
            tenant_id: 所属租户。
            session_id: 所属会话。
            audit: 审计写入口。
            store: 控制面存储，用量入账用。
            capture_requests: 是否把含完整提示词的事件写进审计。
        """
        self._engine = engine
        self._tenant_id = tenant_id
        self._session_id = session_id
        self._audit = audit
        self._store = store
        self._capture_requests = capture_requests
        # 引擎每次被创建是一条新的事件流，序号从头计
        self._stream_id = "st_" + secrets.token_hex(8)
        self._streams: dict[str, TurnStream] = {}
        self._buffer: list[AuditRecord] = []
        self._expected_delivery = 0
        self._task: asyncio.Task[None] | None = None
        self._flusher: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """建立全量订阅。返回时订阅已经登记到引擎上，之后发出的事件不会漏。"""
        source = self._engine.subscribe_all_envelopes()
        first = asyncio.ensure_future(anext(source))
        # 让出一次：上面的任务跑到它的第一个等待点，订阅登记在那之前完成
        await asyncio.sleep(0)
        self._task = asyncio.ensure_future(self._consume(source, first))
        self._flusher = asyncio.ensure_future(self._flush_periodically())

    def open_stream(self, submission_id: str) -> TurnStream:
        """登记一轮对话的事件流。必须在提交返回之后、任何让出点之前调用。"""
        stream = TurnStream(submission_id)
        self._streams[submission_id] = stream
        return stream

    async def _consume(
        self,
        source: AsyncIterator[taifeng.DeliveredEvent],
        first: asyncio.Future[taifeng.DeliveredEvent],
    ) -> None:
        """消费全量事件，直到引擎关闭。"""
        try:
            try:
                envelope = await first
            except StopAsyncIteration:
                return
            await self._handle(envelope)
            async for envelope in source:
                await self._handle(envelope)
        finally:
            for stream in self._streams.values():
                stream.close()
            self._streams.clear()
            await self._flush()

    async def _handle(self, envelope: taifeng.DeliveredEvent) -> None:
        """处理一条事件：查漏、分发、记审计、入账。"""
        event = envelope.event
        if envelope.delivery_seq != self._expected_delivery:
            # 投递序号跳号 = 内核因队列满丢了事件。审计不完整必须留痕
            self._buffer.append(
                self._record(
                    "audit_gap",
                    event,
                    {"expected": self._expected_delivery, "received": envelope.delivery_seq},
                    seq=None,
                )
            )
        self._expected_delivery = envelope.delivery_seq + 1

        stream = self._streams.get(event.submission_id)
        if stream is not None:
            stream.push(event)
            if is_terminal(event) or stream.overflowed:
                self._streams.pop(event.submission_id, None)

        kind = event.msg.kind
        if kind not in STREAMING_KINDS and (kind not in SENSITIVE_KINDS or self._capture_requests):
            self._buffer.append(self._record(kind, event, dict(event.msg.data), seq=event.seq))
        if is_terminal(event):
            await self._account(event)
            await self._flush()
        elif len(self._buffer) >= _FLUSH_SIZE:
            await self._flush()

    def _record(
        self, kind: str, event: taifeng.EventMsg, data: dict[str, Any], *, seq: int | None
    ) -> AuditRecord:
        """事件 → 审计记录。"""
        return AuditRecord(
            tenant_id=self._tenant_id,
            source="engine",
            kind=kind,
            at=event.timestamp.timestamp(),
            actor="engine",
            session_id=self._session_id,
            stream=self._stream_id if seq is not None else None,
            seq=seq,
            data={"submission_id": event.submission_id, **sanitize(data)},
        )

    async def _account(self, event: taifeng.EventMsg) -> None:
        """一轮对话结束时把用量入账。优先用含子 skill 在内的整棵树的用量。"""
        data = event.msg.data
        usage = data.get("subtree_usage") or data.get("usage") or {}
        if not isinstance(usage, dict):
            return
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        total = int(usage.get("total_tokens") or 0) or input_tokens + output_tokens
        try:
            await self._store.add_usage(
                self._tenant_id,
                usage_day(event.timestamp.timestamp()),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total,
                turns=1,
            )
        except Exception:
            logger.exception("会话 %s 的用量入账失败", self._session_id)

    async def _flush(self) -> None:
        """把缓冲里的审计记录写出去。写失败时放回缓冲，等下一次重试。"""
        if not self._buffer:
            return
        batch, self._buffer = self._buffer, []
        try:
            await self._audit.engine(batch)
        except Exception:
            logger.exception(
                "会话 %s 的 %d 条审计记录写入失败，稍后重试", self._session_id, len(batch)
            )
            self._buffer = batch + self._buffer

    async def _flush_periodically(self) -> None:
        """定时把攒着的审计记录写出去。"""
        while True:
            await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)
            await self._flush()

    async def close(self) -> None:
        """停止分发并写完缓冲。引擎关闭后调用。"""
        if self._flusher is not None:
            self._flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flusher
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._flush()


__all__ = ["TERMINAL_KINDS", "EngineEventHub", "SubmissionIndex", "TurnStream", "is_terminal"]
