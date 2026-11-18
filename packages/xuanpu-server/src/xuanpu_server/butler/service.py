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
import json
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from xuanpu_server.assistant import RouteBody
from xuanpu_server.butler.dispatcher import Dispatcher
from xuanpu_server.butler.engine import ButlerEngines
from xuanpu_server.butler.groups.rater import ButlerDivergenceRater
from xuanpu_server.butler.groups.runner import GroupRunner
from xuanpu_server.butler.groups.trace import TracedGroupStore
from xuanpu_server.butler.pending import PendingService
from xuanpu_server.butler.review import Reviewer
from xuanpu_server.butler.tools import ButlerTools, has_draft
from xuanpu_server.butler.turn import ButlerTurn, TurnRegistry
from xuanpu_server.butler.wake_apply import WakeEffects
from xuanpu_server.butler.wakeup import Wakeup
from xuanpu_server.errors import PlatformError, RuntimeUnavailableError
from xuanpu_server.ids import new_id
from xuanpu_server.work_tasks.models import InputFile  # noqa: TC001 —— Pydantic 运行时解析

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from xuanpu_server.assistant_reminders import RouteOutcome
    from xuanpu_server.platform import Platform
    from xuanpu_server.runtime.models import ModelClientFactory
    from xuanpu_server.tenancy import Principal

logger = logging.getLogger(__name__)
HISTORY_MESSAGES = 6


class ButlerMessageBody(BaseModel):
    """员工对管家说的一句话。客户端不能指定租户、匠人或预算。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.:-]+$")
    text: str = Field(min_length=1, max_length=8000, pattern=r"\S")
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    input_files: list[InputFile] = Field(default_factory=list, max_length=8)

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
        self.dispatcher = Dispatcher(platform, now=now)
        self.pending = PendingService(platform, now=now)
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
                             Reviewer(platform, self.engines), now=now, groups=self.groups)
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        # 进行中的轮次：键为 租户:员工:request_id；同标识重试直接回进行中回执，不再跑
        self._running: dict[str, _RunningTurn] = {}

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
        key = f"{who.tenant_id}:{who.user_id}:{body.request_id}"
        running = self._running.get(key)
        if running is None:
            running = _RunningTurn(trace_id=new_id("trc"))
            running.task = asyncio.create_task(self._serve(who, body, running))
            self._running[key] = running
            running.task.add_done_callback(lambda t, k=key: self._finished(k, t))
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

    def _finished(self, key: str, task: asyncio.Task[dict[str, Any]]) -> None:
        """后台轮次结束：移出登记；没人等待时取走异常（已在 ``_serve`` 里记日志并写进主对话）。"""
        self._running.pop(key, None)
        if not task.cancelled() and task.exception() is not None:
            logger.info("管家后台轮次以错误结束：%s", type(task.exception()).__name__)

    async def _serve(self, who: Principal, body: ButlerMessageBody,
                     running: _RunningTurn) -> dict[str, Any]:
        """后台的一轮：认领、续租、处理、完成。回复窗口过后失败的，在主对话写明并保留原话。"""
        p = self._platform
        lock = self._locks.setdefault(f"{who.tenant_id}:{who.user_id}:{body.request_id}",
                                      asyncio.Lock())
        async with lock:
            # 先保存原话：之后任何一步失败，员工的输入都还在主对话里，可以按同一标识重试
            await p.workspace.append_message(
                who, "user", body.text, body.request_id + ":user",
                attachments=[f.item_id for f in body.input_files])
            claim = await p.workspace.claim_route(
                who, "butler:" + body.request_id, body.model_dump(exclude={"request_id"}))
            if claim["state"] == "complete":
                return dict(claim["outcome"])
            renewer = asyncio.create_task(self._renew(who, body.request_id, claim["token"]))
            try:
                receipt = await self._handle(who, body, running.trace_id)
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
        from xuanpu_server.api.assistant import route_view

        outcome = await self._platform.assistant.route(who, RouteBody(
            text=body.text, request_id=body.request_id, timezone=body.timezone,
            attachments=[f.item_id for f in body.input_files], input_files=body.input_files))
        return {"mode": "route", **route_view(outcome)}

    async def _handle(self, who: Principal, body: ButlerMessageBody,
                      trace_id: str) -> dict[str, Any]:
        """跑一轮管家并写助手消息；失败统一成可重试的错误，输入已保留。"""
        p, tenant = self._platform, who.require_tenant()
        await p.quota.check_new_turn(tenant)
        turn = ButlerTurn(who=who, request_id=body.request_id, trace_id=trace_id,
                          timezone=body.timezone, materials=tuple(body.input_files),
                          user_text=body.text)
        await p.sessions.admit_routing(tenant)
        try:
            reply = await self.engines.run(turn, await self._payload(who, body))
        except PlatformError:
            raise
        except Exception:
            logger.exception("管家处理失败")
            raise RuntimeUnavailableError("管家暂时无法处理，输入已保留，请稍后重试") from None
        finally:
            p.sessions.release_routing(tenant)
        outcome = self._outcome(turn, reply)
        saved = outcome.saved()
        await p.workspace.append_message(
            who, "assistant", outcome.message or "请确认操作。", body.request_id + ":assistant",
            metadata={"route": saved, "butler": {"trace_id": turn.trace_id}})
        return {"mode": "butler", "state": "done", "request_id": body.request_id,
                "trace_id": turn.trace_id,
                **saved, "dispatches": [_dispatch_view(r) for r in turn.dispatches],
                "groups": list(turn.groups),
                "answered_pending": list(turn.answered)}

    def _outcome(self, turn: ButlerTurn, reply: str) -> RouteOutcome:
        """回复文本为准；有草稿时带上卡片并以本请求作为待确认标识。"""
        from xuanpu_server.assistant_reminders import RouteOutcome

        if turn.proposal is None:
            return RouteOutcome(message=reply or None)
        return replace(turn.proposal, message=reply or turn.proposal.message,
                       proposal_id=turn.request_id if has_draft(turn.proposal) else None)

    async def _payload(self, who: Principal, body: ButlerMessageBody) -> str:
        """本轮输入：最近对话、当前时间与原话，全部作为数据交给管家。"""
        recent = await self._platform.workspace.messages(who, limit=HISTORY_MESSAGES + 1,
                                                         latest=True, private_only=True)
        history = [{"role": m["role"], "text": m["text"][-2000:]} for m in recent
                   if m["request_id"] != body.request_id + ":user"][-HISTORY_MESSAGES:]
        questions = [{"pending_id": i.pending_id, "question": i.question}
                     for i in await self.pending.list(who) if i.kind == "question"]
        return json.dumps({
            "now": datetime.fromtimestamp(self._now(), ZoneInfo(body.timezone)).isoformat(),
            "timezone": body.timezone, "history": history, "pending_questions": questions,
            "materials": [f.model_dump() for f in body.input_files], "text": body.text,
        }, ensure_ascii=False)

    def start(self) -> None:
        """启动唤醒循环。"""
        self.wakeup.start()

    async def close(self) -> None:
        """先停后台轮次与唤醒，再关引擎池。被取消的一轮不写半截回复，认领随之释放。"""
        running = [r.task for r in self._running.values()]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await self.wakeup.close()
        await self.dispatcher.close()
        await self.pending.close()
        await self.engines.close()



def _dispatch_view(record: Any) -> dict[str, Any]:
    """回执里的派工：只给标识、匠人与状态。"""
    return {"dispatch_id": record.dispatch_id, "package": record.artisan.package,
            "entry": record.artisan.entry, "state": record.state, "host": record.host}


_RENEW_SECONDS = 20.0
_LEASE_SECONDS = 60.0


@dataclass
class _RunningTurn:
    """进行中的一轮：后台任务、预先确定的 trace 标识、员工是否已经不再等待。"""

    trace_id: str
    task: asyncio.Task[dict[str, Any]] = field(init=False)
    detached: bool = False
