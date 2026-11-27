"""匠人会话与工作任务的本地双层 Transcript 实时录制器（ADR 0026）。

- 挂接在 ``EngineEventHub`` 上，把任意匠人会话（``artisan``）与持久工作任务（``work_task``）
  的每轮事件脱敏落盘到 ``compact.jsonl`` + ``full.jsonl``，轮次结束时异步归档至共享
  ``ObjectStore`` 并登记全局 ``transcript_index``；
- 若存在跨引擎绑定（``TraceWiring.binder``），自动挂在同一条管家 ``trace_id`` 与派工
  ``parent_span_id`` 下，实现「管家 + 匠人全链路同 trace 跨节点可查」。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from server.engine.transcripts.models import TranscriptIndexRecord, TranscriptKind, TranscriptStatus
from server.engine.transcripts.writer import LocalTranscriptWriter

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import taifeng

    from server.core.controlplane.store import ControlPlaneStore
    from server.core.objectstore import ObjectStore
    from server.engine.tracing.config import TraceWiring

logger = logging.getLogger(__name__)
_RECORDED_KINDS = frozenset({
    "turn_started",
    "tool_call_started",
    "tool_call_completed",
    "turn_completed",
    "turn_failed",
    "turn_suspended",
})


@dataclass(frozen=True)
class TranscriptSinkConfig:
    """会话/任务 Transcript 落盘所需的平台级依赖。"""

    data_dir: Path
    objects: ObjectStore
    node_id: str
    node_addr: str | None = None
    now: Callable[[], float] = time.time


class SessionTranscriptRecorder:
    """按 ``submission_id`` 为每个匠人/任务轮次维护本地双层 Transcript 并索引归档。"""

    def __init__(
        self,
        *,
        sink: TranscriptSinkConfig,
        store: ControlPlaneStore,
        tenant_id: str,
        session_id: str,
        user_id: str | None,
        kind: TranscriptKind,
        tracing: TraceWiring | None = None,
    ) -> None:
        """初始化录制器；按需惰性解析跨引擎 trace 绑定。"""
        self._sink = sink
        self._store = store
        self._tenant_id = tenant_id
        self._session_id = session_id
        self._user_id = user_id
        self._kind: TranscriptKind = kind
        self._tracing = tracing
        self._writers: dict[str, LocalTranscriptWriter] = {}
        self._binding_resolved = False
        self._trace_id: str = f"sess:{session_id}"
        self._parent_span_id: str | None = None

    async def _resolve_binding(self) -> tuple[str, str | None]:
        """优先查跨引擎绑定以归并到管家 trace，查不到时以 ``sess:<session_id>`` 兜底。"""
        if self._binding_resolved:
            return self._trace_id, self._parent_span_id
        self._binding_resolved = True
        if (
            self._tracing is not None
            and self._tracing.binder is not None
            and self._user_id is not None
        ):
            try:
                from server.engine.tracing.models import TraceOwner

                owner = TraceOwner(tenant_id=self._tenant_id, user_id=self._user_id)
                binding = await self._tracing.binder.bind(owner, self._session_id)
                if binding is not None:
                    self._trace_id = binding.trace_id
                    self._parent_span_id = binding.parent_span_id
            except Exception:
                logger.exception("解析会话 %s 的跨引擎 trace 绑定失败，降级使用会话级 trace", self._session_id)
        return self._trace_id, self._parent_span_id

    async def _writer_for(self, submission_id: str) -> LocalTranscriptWriter:
        """取出或惰性创建本轮的本地双层 Transcript 写入器。"""
        writer = self._writers.get(submission_id)
        if writer is not None:
            return writer
        trace_id, parent_span = await self._resolve_binding()
        span_id = f"{self._session_id}_{submission_id}"[:160]
        now_ts = self._sink.now()
        writer = LocalTranscriptWriter(
            self._sink.data_dir,
            tenant_id=self._tenant_id,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span,
            at=datetime.fromtimestamp(now_ts, UTC),
        )
        self._writers[submission_id] = writer
        return writer

    async def on_event(self, event: taifeng.EventMsg) -> None:
        """记录关键执行事件（跳过高频流式增量，正文在终态统一落盘）。"""
        kind = event.msg.kind
        if kind not in _RECORDED_KINDS:
            return
        try:
            writer = await self._writer_for(event.submission_id)
            data: dict[str, Any] = dict(event.msg.data)
            status = "error" if (kind == "tool_call_completed" and data.get("is_error")) else "ok"
            await writer.append(kind=kind, status=status, payload=data, ts=event.timestamp.timestamp())
        except Exception:
            logger.exception("写入匠人 Transcript 事件失败（session=%s）", self._session_id)

    async def finish_turn(
        self,
        submission_id: str,
        *,
        text: str,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        """轮次结束时落盘完整回复、上传冷归档并登记全局元数据索引。"""
        writer = self._writers.pop(submission_id, None)
        if writer is None:
            if not text:
                return
            writer = await self._writer_for(submission_id)
            self._writers.pop(submission_id, None)
        now_ts = self._sink.now()
        try:
            if text:
                await writer.append(kind="assistant_reply", payload={"text": text}, ts=now_ts)
        except Exception:
            logger.exception("落盘匠人最终回复 Transcript 失败（session=%s）", self._session_id)

        status: TranscriptStatus = (
            "completed" if outcome == "completed"
            else "cancelled" if outcome == "cancelled"
            else "failed"
        )
        compact_key: str | None = None
        full_key: str | None = None
        archived_at: datetime | None = None
        try:
            compact_key, full_key = await writer.archive(self._sink.objects)
            archived_at = datetime.fromtimestamp(self._sink.now(), UTC)
        except Exception:
            logger.exception("归档匠人 Transcript 失败（保留本地热盘，span=%s）", writer.span_id)

        record = TranscriptIndexRecord(
            tenant_id=self._tenant_id,
            span_id=writer.span_id,
            trace_id=writer.trace_id,
            parent_span_id=writer.parent_span_id,
            session_id=self._session_id,
            user_id=self._user_id,
            kind=self._kind,
            node_id=self._sink.node_id,
            node_addr=self._sink.node_addr,
            local_relpath=writer.relpath,
            compact_object_key=compact_key,
            full_object_key=full_key,
            byte_size=writer.byte_size,
            line_count=writer.line_count,
            status=status,
            error_code=error_code,
            created_at=writer.created_at,
            archived_at=archived_at,
        )
        try:
            await self._store.transcript_store.upsert(record)
        except Exception:
            logger.exception("写入匠人 Transcript 索引失败（span=%s）", writer.span_id)
