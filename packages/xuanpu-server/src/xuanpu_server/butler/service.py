"""管家入口服务：``POST /v1/assistant/messages`` 背后的逻辑。

- 租户未开启管家（或未同时开启执行追踪）时，原样走既有分流，回执标 ``mode=route``。
- 开启时：先把员工原话写进主对话（失败也不丢输入），按 ``request_id`` 认领（同键同体回原回执，
  不同体 409），跑一轮管家，写回执与助手消息。派工只建记录并启动匠人，不等结果。
- 提出的草稿与分流产出的卡片同形，沿用既有的待确认与确认接口。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from xuanpu_server.assistant import RouteBody
from xuanpu_server.butler.dispatcher import Dispatcher
from xuanpu_server.butler.engine import ButlerEngines
from xuanpu_server.butler.groups.runner import GroupRunner
from xuanpu_server.butler.groups.trace import TracedGroupStore
from xuanpu_server.butler.pending import PendingService
from xuanpu_server.butler.review import Reviewer
from xuanpu_server.butler.tools import ButlerTools
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
        self.wakeup = Wakeup(platform, WakeEffects(platform, self.pending, now=now,
                                                   groups=self.groups),
                             Reviewer(platform, self.engines), now=now, groups=self.groups)
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def enabled(self, tenant: str) -> bool:
        """管家与执行追踪同时对该租户开启才走管家（派工预算与执行树依赖追踪）。"""
        p = self._platform
        return p.butler_config.allows(tenant) and p.runtimes.defaults.execution_trace.allows(tenant)

    async def message(self, who: Principal, body: ButlerMessageBody) -> dict[str, Any]:
        """处理一句话，返回回执。未开启的租户走既有分流。"""
        if who.tenant_id is None or not self.enabled(who.tenant_id) or not who.user_id:
            return await self._route(who, body)
        p = self._platform
        await p.workspace.require_employee(who)
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
            try:
                receipt = await self._handle(who, body)
                await p.workspace.complete_route(who, "butler:" + body.request_id,
                                                 claim["token"], receipt)
                return receipt
            finally:
                await p.workspace.release_route(who, "butler:" + body.request_id, claim["token"])

    async def _route(self, who: Principal, body: ButlerMessageBody) -> dict[str, Any]:
        """降级：既有分流，回执与 ``/v1/assistant/route`` 同形。"""
        from xuanpu_server.api.assistant import route_view

        outcome = await self._platform.assistant.route(who, RouteBody(
            text=body.text, request_id=body.request_id, timezone=body.timezone,
            attachments=[f.item_id for f in body.input_files], input_files=body.input_files))
        return {"mode": "route", **route_view(outcome)}

    async def _handle(self, who: Principal, body: ButlerMessageBody) -> dict[str, Any]:
        """跑一轮管家并写助手消息；失败统一成可重试的错误，输入已保留。"""
        p, tenant = self._platform, who.require_tenant()
        await p.quota.check_new_turn(tenant)
        turn = ButlerTurn(who=who, request_id=body.request_id, trace_id=new_id("trc"),
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
        return {"mode": "butler", "request_id": body.request_id, "trace_id": turn.trace_id,
                **saved, "dispatches": [_dispatch_view(r) for r in turn.dispatches],
                "groups": list(turn.groups),
                "answered_pending": list(turn.answered)}

    def _outcome(self, turn: ButlerTurn, reply: str) -> RouteOutcome:
        """回复文本为准；有草稿时带上卡片并以本请求作为待确认标识。"""
        from xuanpu_server.assistant_reminders import RouteOutcome

        if turn.proposal is None:
            return RouteOutcome(message=reply or None)
        return replace(turn.proposal, message=reply or turn.proposal.message,
                       proposal_id=turn.request_id if _has_draft(turn.proposal) else None)

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
        """先停唤醒与后台读取，再关引擎池。"""
        await self.wakeup.close()
        await self.dispatcher.close()
        await self.pending.close()
        await self.engines.close()


def _has_draft(outcome: RouteOutcome) -> bool:
    """是否带待确认草稿（只打开列表的不算）。"""
    return any((outcome.reminder_draft, outcome.workspace_draft, outcome.proactive_draft,
                outcome.task_definition_draft, outcome.shared_draft, outcome.handoff_draft))


def _dispatch_view(record: Any) -> dict[str, Any]:
    """回执里的派工：只给标识、匠人与状态。"""
    return {"dispatch_id": record.dispatch_id, "package": record.artisan.package,
            "entry": record.artisan.entry, "state": record.state, "host": record.host}
