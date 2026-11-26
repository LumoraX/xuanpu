"""管家引擎：每个租户一个内核引擎池，只装内置管家技能与管家工具。

- 不加载任何业务包代码，没有沙盒、脚本执行器、连接器或网络工具；只用 ``taifeng.__all__``。
- 每次员工输入跑一轮：新建一个引擎、接上平台事件枢纽（审计、按员工计量、执行追踪；
  根执行 span 类型为 butler）、提交、读到本轮终态、释放引擎。员工的长期对话是主对话的
  持久记录，每轮把最近几条作为数据带入，不依赖引擎线程跨进程续接。
- 工具按内核线程标识找回本轮上下文。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import taifeng

from server.core.errors import RuntimeUnavailableError
from server.core.tenancy import tenant_directory
from server.engine.runtime.events import EngineEventHub
from server.engine.tracing.events import execution_span
from server.engine.transcripts.models import TranscriptIndexRecord, TranscriptStatus
from server.engine.transcripts.writer import LocalTranscriptWriter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from server.engine.butler.tools import ButlerTools
    from server.engine.butler.turn import ButlerTurn, TurnRegistry
    from server.engine.runtime.models import ModelClientFactory
    from server.platform import Platform

logger = logging.getLogger(__name__)
SKILLS_DIR = Path(__file__).parent / "skills"
SKILL_ID = "butler"
MODEL_TIER = "balanced"
MAX_ITERATIONS = 12
MAX_SESSION_TOKENS = 200_000


class TurnTimeout(Exception):  # noqa: N818 —— 是一种正常结局，不是错误
    """一轮到了时限：调用方正常收尾（已派出的工作继续），不报不可用。"""

    def __init__(self, elapsed: float) -> None:
        """记下实际跑了多久。"""
        super().__init__(f"管家一轮到时限，已运行 {elapsed:.0f} 秒")
        self.elapsed = elapsed


class ButlerEngines:
    """按租户懒建管家引擎池。"""

    def __init__(self, platform: Platform, models: ModelClientFactory, tools: ButlerTools,
                 turns: TurnRegistry, *, data_dir: Path) -> None:
        """依赖由平台注入。"""
        self._platform = platform
        self._models = models
        self._tools = tools
        self._turns = turns
        self._data_dir = data_dir
        self._pools: dict[str, taifeng.EnginePool] = {}
        self._lock = asyncio.Lock()

    async def pool(self, tenant: str) -> taifeng.EnginePool:
        """取（必要时新建）租户的管家引擎池。"""
        async with self._lock:
            existing = self._pools.get(tenant)
            if existing is not None:
                return existing
            threads = self._data_dir / "butler" / tenant_directory(tenant) / "threads"
            await asyncio.to_thread(threads.mkdir, parents=True, exist_ok=True)
            pool = await taifeng.EnginePool.create(
                skills_dir=SKILLS_DIR, threads_dir=threads,
                model_client=await self._models.create(tenant, MODEL_TIER),
                extra_tools=self._tools.specs(), script_executors={},
                max_iterations=MAX_ITERATIONS, max_session_tokens=MAX_SESSION_TOKENS,
                doom_loop_config=taifeng.DoomLoopConfig(
                    max_consecutive_repeats=3,
                    max_consecutive_errors=2,
                ),
                enable_request_capture=self._platform.runtimes.defaults.capture_requests,
            )
            self._pools[tenant] = pool
            return pool

    async def run(self, turn: ButlerTurn, payload: str) -> str:
        """跑一轮，返回管家给员工的回复文本。

        Raises:
            TurnTimeout: 本轮到了时限（默认 ``turn_seconds``，管家可延长）。
            RuntimeUnavailableError: 本轮没有正常完成（失败、挂起或回复为空）。
        """
        tenant = turn.who.require_tenant()
        pool = await self.pool(tenant)
        session_id = "bt_" + secrets.token_hex(12)
        engine = await pool.get_or_create(session_id=session_id, entry_skill_id=SKILL_ID)
        self._turns.bind(engine.thread_id, turn)
        hub: EngineEventHub | None = None
        timed_out = False
        now_ts = self._platform._now()
        writer = LocalTranscriptWriter(
            self._data_dir,
            tenant_id=tenant,
            trace_id=turn.trace_id,
            span_id=session_id,
            parent_span_id=turn.root_span,
            at=datetime.fromtimestamp(now_ts, UTC),
        )
        await writer.append(kind="user_input", payload={"text": payload}, ts=now_ts)
        final_status: TranscriptStatus = "completed"
        error_code: str | None = None
        try:
            hub = self._hub(engine, turn, session_id)
            await hub.start()
            submission = await engine.submit(taifeng.UserMessage(text=payload))
            stream = hub.open_stream(submission)
            seconds = self._platform.butler_config.turn_seconds
            try:
                async with asyncio.timeout(seconds) as deadline:
                    turn.deadline = deadline
                    turn.started_at = asyncio.get_running_loop().time()
                    reply = await _read_reply(stream.events(), turn, writer=writer)
                    await writer.append(kind="assistant_reply", payload={"text": reply}, ts=self._platform._now())
                    return reply
            except TimeoutError:
                # 只把「本轮时限到了」报成 TurnTimeout；别处冒出的 TimeoutError 照旧往上抛
                if deadline.expired():
                    timed_out = True
                    final_status = "cancelled"
                    error_code = "turn_timeout"
                    await writer.append(
                        kind="error", status="cancelled",
                        payload={"error_code": "turn_timeout"}, ts=self._platform._now(),
                    )
                    raise TurnTimeout(asyncio.get_running_loop().time() - turn.started_at) from None
                raise
        except TurnTimeout:
            raise
        except Exception as exc:
            final_status = "failed"
            error_code = type(exc).__name__
            await writer.append(
                kind="error", status="failed",
                payload={"error_type": type(exc).__name__, "message": str(exc)},
                ts=self._platform._now(),
            )
            raise
        finally:
            self._turns.unbind(engine.thread_id)
            await pool.release(session_id, force=True)
            await self._persist_transcript_index(turn, writer, session_id, final_status, error_code)
            if hub is not None:
                if timed_out:
                    await hub.close(unfinished_state="cancelled", unfinished_reason="turn_timeout")
                else:
                    await hub.close()

    async def _persist_transcript_index(
        self,
        turn: ButlerTurn,
        writer: LocalTranscriptWriter,
        session_id: str,
        status: TranscriptStatus,
        error_code: str | None,
    ) -> None:
        """登记本地 Transcript 全局元数据索引并异步归档到对象存储；异常记日志不覆盖业务结果。"""
        p = self._platform
        tenant = turn.who.require_tenant()
        compact_key: str | None = None
        full_key: str | None = None
        archived_at: datetime | None = None
        try:
            compact_key, full_key = await writer.archive(p.objects)
            archived_at = datetime.fromtimestamp(p._now(), UTC)
        except Exception:
            logger.exception("归档管家 Transcript %s 失败（保留本地热盘）", session_id)
        record = TranscriptIndexRecord(
            tenant_id=tenant,
            span_id=session_id,
            trace_id=turn.trace_id,
            parent_span_id=turn.root_span,
            session_id=session_id,
            user_id=turn.who.user_id,
            kind="butler",
            node_id=p.instance_id,
            node_addr=p.node_addr,
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
            await p.store.transcript_store.upsert(record)
        except Exception:
            logger.exception("写入管家 Transcript 索引 %s 失败", session_id)

    def _hub(self, engine: taifeng.AgentEngine, turn: ButlerTurn, session_id: str,
             ) -> EngineEventHub:
        """与业务会话同一个事件枢纽：审计、按员工计量、追踪（根 span 为 butler）。"""
        p = self._platform
        wiring = replace(p.runtimes.defaults.trace_wiring, root_kind="butler",
                         trace_id=turn.trace_id)
        return EngineEventHub(
            engine, tenant_id=turn.who.require_tenant(), session_id=session_id,
            audit=p.audit, store=p.store, metrics=p.metrics,
            capture_requests=p.runtimes.defaults.capture_requests, tracing=wiring,
            user_id=turn.who.user_id, users=p.identity.store,
        )

    async def close(self) -> None:
        """关闭全部引擎池；单个失败记日志后继续。"""
        pools, self._pools = list(self._pools.values()), {}
        for pool in pools:
            try:
                await pool.close()
            except Exception:
                logger.exception("关闭管家引擎池失败")


_TOOL_STEP_LABELS = {
    "read_personal": "正在查询个人档案与工作台记录…",
    "act_personal": "正在为你整理并办理私人事务…",
    "propose": "正在整理待确认卡片…",
    "list_artisans": "正在查看可用匠人…",
    "dispatch_artisan": "正在派发任务给匠人…",
    "dispatch_group": "正在编排多位匠人协作…",
    "check_dispatch": "正在查看匠人进展…",
    "check_group": "正在查看编组进展…",
    "answer_pending": "正在把你的答复转给匠人…",
}


async def _read_reply(
    events: AsyncIterator[taifeng.EventMsg],
    turn: ButlerTurn,
    *,
    writer: LocalTranscriptWriter | None = None,
) -> str:
    """读本轮事件：记下根执行 span，实时上报步骤、落盘双层 Transcript 与回复文本，按终态判定成败。"""
    text: list[str] = []
    async for event in events:
        identity = event.execution
        kind = event.msg.kind
        ts = event.timestamp.timestamp()
        if kind == "turn_started" and identity is not None and identity.parent_execution_id is None:
            turn.note_root(execution_span(identity.execution_id))
            turn.note_step("正在理解你的请求…")
        elif kind == "tool_call_started":
            tool_name = str(event.msg.data.get("name", ""))
            turn.note_step(_TOOL_STEP_LABELS.get(tool_name, "正在调用内置工具处理…"))
            if writer is not None:
                await writer.append(kind="tool_call_started", payload=dict(event.msg.data), ts=ts)
        elif kind == "tool_call_completed":
            turn.note_step("处理完成，正在组织回复…")
            if writer is not None:
                data: dict[str, Any] = dict(event.msg.data)
                await writer.append(
                    kind="tool_call_completed",
                    status="error" if data.get("is_error") else "ok",
                    payload=data, ts=ts,
                )
        elif kind == "assistant_text":
            delta = str(event.msg.data.get("delta", ""))
            text.append(delta)
            turn.heard(delta)
        elif kind == "turn_completed":
            if writer is not None:
                await writer.append(kind="turn_completed", payload=dict(event.msg.data), ts=ts)
            if event.msg.data.get("end_reason") == "doom_loop_circuit_open" and not turn.has_output():
                raise RuntimeUnavailableError("管家连续重试同一操作未能完成，输入已保留，请稍后重试")
            reply = "".join(text).strip()
            if not reply and not turn.has_output():
                raise RuntimeUnavailableError("管家没有给出答复，请稍后重试")
            return reply
        elif kind in ("turn_failed", "turn_suspended"):
            if writer is not None:
                await writer.append(kind=kind, status="failed", payload=dict(event.msg.data), ts=ts)
            raise RuntimeUnavailableError("管家暂时无法处理，输入已保留，请稍后重试")
    raise RuntimeUnavailableError("管家处理中断，输入已保留，请稍后重试")
