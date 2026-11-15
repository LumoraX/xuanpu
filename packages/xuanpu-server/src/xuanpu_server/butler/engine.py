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
from pathlib import Path
from typing import TYPE_CHECKING

import taifeng

from xuanpu_server.errors import RuntimeUnavailableError
from xuanpu_server.runtime.events import EngineEventHub
from xuanpu_server.tenancy import tenant_directory
from xuanpu_server.tracing.events import execution_span

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from xuanpu_server.butler.tools import ButlerTools
    from xuanpu_server.butler.turn import ButlerTurn, TurnRegistry
    from xuanpu_server.platform import Platform
    from xuanpu_server.runtime.models import ModelClientFactory

logger = logging.getLogger(__name__)
SKILLS_DIR = Path(__file__).parent / "skills"
SKILL_ID = "butler"
MODEL_TIER = "balanced"
MAX_ITERATIONS = 12
MAX_SESSION_TOKENS = 200_000
TURN_SECONDS = 90.0


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
                enable_request_capture=self._platform.runtimes.defaults.capture_requests,
            )
            self._pools[tenant] = pool
            return pool

    async def run(self, turn: ButlerTurn, payload: str) -> str:
        """跑一轮，返回管家给员工的回复文本。

        Raises:
            RuntimeUnavailableError: 本轮没有正常完成（失败、挂起、超时或回复为空）。
        """
        tenant = turn.who.require_tenant()
        pool = await self.pool(tenant)
        session_id = "bt_" + secrets.token_hex(12)
        engine = await pool.get_or_create(session_id=session_id, entry_skill_id=SKILL_ID)
        self._turns.bind(engine.thread_id, turn)
        hub: EngineEventHub | None = None
        try:
            hub = self._hub(engine, turn, session_id)
            await hub.start()
            submission = await engine.submit(taifeng.UserMessage(text=payload))
            stream = hub.open_stream(submission)
            async with asyncio.timeout(TURN_SECONDS):
                return await _read_reply(stream.events(), turn)
        finally:
            self._turns.unbind(engine.thread_id)
            await pool.release(session_id, force=True)
            if hub is not None:
                await hub.close()

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


async def _read_reply(events: AsyncIterator[taifeng.EventMsg], turn: ButlerTurn) -> str:
    """读本轮事件：记下根执行 span，拼出回复文本，按终态判定成败。"""
    text: list[str] = []
    async for event in events:
        identity = event.execution
        kind = event.msg.kind
        if kind == "turn_started" and identity is not None and identity.parent_execution_id is None:
            turn.note_root(execution_span(identity.execution_id))
        elif kind == "assistant_text":
            text.append(str(event.msg.data.get("delta", "")))
        elif kind == "turn_completed":
            reply = "".join(text).strip()
            if not reply and not turn.has_output():
                raise RuntimeUnavailableError("管家没有给出答复，请稍后重试")
            return reply
        elif kind in ("turn_failed", "turn_suspended"):
            raise RuntimeUnavailableError("管家暂时无法处理，输入已保留，请稍后重试")
    raise RuntimeUnavailableError("管家处理中断，输入已保留，请稍后重试")
