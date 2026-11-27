"""管家入口：``POST /v1/assistant/messages`` 与待本人处理项。

开启管家的租户由管家处理（自办或派给一位匠人，不等匠人结果）；其余租户走既有分流。
``request_id`` 必填：同键同体回原回执，不同体 409。

流式回执（R7）：请求带 ``Accept: text/event-stream`` 时以 SSE 返回：``started``（trace 标识，立刻
推送）→ ``delta``（管家回复的文本增量）→ ``receipt``（与 JSON 回执相同）或 ``error``（错误码与文案）。
未开启管家的租户只推一帧 ``receipt``。身份与请求体校验在建流前完成，出错照常以 HTTP 状态返回。

本人可用的匠人（R7）：``GET /v1/assistant/artisans``，小院据此让匠人在垫子上待命。

待本人处理项（R5-C2）：``GET /v1/assistant/pending`` 列出本人仍待处理的问题与审批提示；
``POST /v1/assistant/pending/{id}/answer`` 答复问题，原话转给匠人。审批类项请在既有待确认里处理。
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析
from server.api.localization import request_locale
from server.core.errors import NotFoundError
from server.engine.butler.grant_converger import (
    MAX_BUDGET_TOKENS,
    MIN_BUDGET_TOKENS,
    converge_grant_from_trace,
)
from server.engine.butler.pending import view
from server.engine.butler.personal_actions import undo_executed_action
from server.engine.butler.service import (
    ButlerMessageBody,  # noqa: TC001 —— FastAPI 运行时解析
)
from server.engine.butler.topic_watch import evaluate_watch_signal
from server.engine.tracing.models import TraceOwner
from spec.localization import display_name_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from server.platform import Platform


def _frame(name: str, data: dict[str, Any]) -> str:
    """一帧 SSE。"""
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class PendingAnswerBody(BaseModel):
    """员工对一个待处理问题的答复。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.:-]+$")
    text: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class ConvergeGrantBody(BaseModel):
    """试运行 Trace 收敛最小权限 Grant 的可选参数。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    budget_ceiling: int = Field(default=MAX_BUDGET_TOKENS, ge=MIN_BUDGET_TOKENS, le=MAX_BUDGET_TOKENS)


class TopicWatchCreateBody(BaseModel):
    """登记长期静默追踪主题的请求体。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    topic: str = Field(min_length=1, max_length=200, pattern=r"\S")


class TopicWatchEvaluateBody(BaseModel):
    """评估一次静默追踪动态信号的请求体。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    signal: str | None = Field(default=None, max_length=4000)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    deliver: bool = False
    daily_bg_tokens_used: int = Field(default=0, ge=0)


def butler_router(platform: Platform) -> APIRouter:
    """注册管家入口与待处理项接口。"""
    router = APIRouter()

    @router.post("/v1/assistant/messages", response_model=None)
    async def message(body: ButlerMessageBody, who: Caller,
                      request: Request) -> dict[str, Any] | StreamingResponse:
        """员工对管家说一句话；返回回执（``mode`` 为 butler 或 route），要流时以 SSE 推送。"""
        if "text/event-stream" not in request.headers.get("accept", ""):
            return await platform.butler.message(who, body)
        opened = await platform.butler.open_stream(who, body)

        async def frames() -> AsyncIterator[str]:
            """逐帧编码；分流回执只有一帧。"""
            if isinstance(opened, dict):
                yield _frame("receipt", opened)
                return
            async for name, data in platform.butler.stream_events(body, opened):
                yield _frame(name, data)

        return StreamingResponse(frames(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @router.get("/v1/assistant/artisans")
    async def artisans(request: Request, who: Caller) -> dict[str, Any]:
        """本人此刻可用的匠人（包名、入口、说明）；只给当前启用的员工。"""
        await platform.workspace.require_employee(who)
        found = await platform.artisans.available(who)
        locale = request_locale(request)
        return {"items": [{"package": a.ref.package, "entry": a.ref.entry,
                            "name": display_name_for(a.display_name, locale) or a.ref.package,
                            "entry_name": display_name_for(a.entry_display_name, locale) or a.ref.entry,
                            "description": a.description, "host": a.host,
                            "accepts_materials": a.host == "task",
                            "connectors": list(a.connectors),
                            "capabilities": {"shell": a.sandbox_shell, "network": a.sandbox_network}}
                           for a in found]}

    @router.get("/v1/assistant/pending")
    async def pending(who: Caller) -> dict[str, Any]:
        """本人仍待处理的项（不含已答复、已解决与代答记录）。"""
        return {"items": [view(i) for i in await platform.butler.pending.list(who)]}

    @router.get("/v1/assistant/task-boards")
    async def task_boards(who: Caller, active_only: bool = False, limit: int = 20) -> dict[str, Any]:
        """本人跨轮持久任务板列表（供纯 Web Live Focus 与 Aux Pane 展示）。"""
        from server.engine.butler.pending import owner_of
        from server.engine.butler.task_board import board_view

        await platform.workspace.require_employee(who)
        owner = owner_of(who)
        bounded = max(1, min(limit, 50))
        store = platform.store.task_board_store
        records = (
            await store.list_active(owner, limit=bounded)
            if active_only
            else await store.list_recent(owner, limit=bounded)
        )
        return {"items": [board_view(b) for b in records]}

    @router.get("/v1/assistant/briefings/daily")
    async def daily_briefing(
        who: Caller, timezone: str = "Asia/Shanghai", deliver: bool = False,
    ) -> dict[str, Any]:
        """按本人时区生成高信噪比今日要点晨报（可选经 Attention Budget 写入主对话）。"""
        await platform.workspace.require_employee(who)
        return await platform.butler.briefings.build_daily_brief(
            who, timezone=timezone, deliver=deliver,
        )

    @router.get("/v1/assistant/briefings/pre-meeting")
    async def pre_meeting_briefings(
        who: Caller,
        timezone: str = "Asia/Shanghai",
        window_minutes: int = 30,
        deliver: bool = False,
    ) -> dict[str, Any]:
        """扫描未来 30 分钟内即将开始的日程，生成会前 1 分钟速览卡。"""
        await platform.workspace.require_employee(who)
        items = await platform.butler.briefings.build_pre_meeting_briefs(
            who, timezone=timezone, window_minutes=window_minutes, deliver=deliver,
        )
        return {"items": items}

    @router.post("/v1/assistant/traces/{trace_id}/converge-grant")
    async def converge_grant(
        trace_id: str, who: Caller, body: ConvergeGrantBody | None = None,
    ) -> dict[str, Any]:
        """从本人一次试运行 Trace 与派工记录收敛最小权限 Grant 提案（严禁通配符）。"""
        tenant_id, user_id = await platform.workspace.require_employee(who)
        owner = TraceOwner(tenant_id=tenant_id, user_id=user_id)
        trace = await platform.store.trace_store.get(owner, trace_id)
        dispatches = await platform.store.dispatch_store.list_for_trace(owner, trace_id)
        ceiling = body.budget_ceiling if body is not None else MAX_BUDGET_TOKENS
        proposal = converge_grant_from_trace(trace, dispatches, budget_ceiling=ceiling)
        return proposal.model_dump(mode="json")

    @router.get("/v1/assistant/topic-watches")
    async def list_topic_watches(who: Caller) -> dict[str, Any]:
        """列出本人登记的全部长期静默追踪主题。"""
        await platform.workspace.require_employee(who)
        items = await platform.butler.topic_watches.list(who)
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.post("/v1/assistant/topic-watches")
    async def create_topic_watch(body: TopicWatchCreateBody, who: Caller) -> dict[str, Any]:
        """登记一条新的长期静默追踪主题并落盘持久化。"""
        await platform.workspace.require_employee(who)
        created = await platform.butler.topic_watches.create(who, body.topic)
        return created.model_dump(mode="json")

    @router.delete("/v1/assistant/topic-watches/{watch_id}")
    async def delete_topic_watch(watch_id: str, who: Caller) -> dict[str, Any]:
        """删除本人一条长期静默追踪主题。"""
        await platform.workspace.require_employee(who)
        await platform.butler.topic_watches.delete(who, watch_id)
        return {"deleted": True, "watch_id": watch_id}

    @router.post("/v1/assistant/topic-watches/{watch_id}/evaluate")
    async def evaluate_topic_watch(
        watch_id: str, body: TopicWatchEvaluateBody, who: Caller,
    ) -> dict[str, Any]:
        """比对新动态与上次已汇报基线：无实质新变化保持静默；有新变化经注意力门控放行并刷新基线。"""
        tenant_id, user_id = await platform.workspace.require_employee(who)
        items = await platform.butler.topic_watches.list(who)
        target = next((item for item in items if item.watch_id == watch_id), None)
        if target is None:
            raise NotFoundError("追踪主题不存在")
        await platform.butler.briefings.hydrate_attention(who)
        habit = await platform.butler.briefings.get_habit(who)
        result = evaluate_watch_signal(
            target,
            body.signal,
            user_key=f"{tenant_id}:{user_id}",
            now=time.time(),
            gate=platform.butler.briefings.gate,
            timezone=body.timezone,
            daily_bg_tokens_used=body.daily_bg_tokens_used,
            habit=habit,
        )
        if result.notify:
            await platform.butler.topic_watches.save(who, result.updated_watch)
            await platform.butler.briefings.persist_attention(who)
            if body.deliver and result.message:
                digest = result.updated_watch.last_signal_digest or "latest"
                await platform.workspace.append_message(
                    who,
                    "assistant",
                    result.message,
                    f"tw:{watch_id}:{digest[:16]}",
                    metadata={"kind": "topic_watch", "watch_id": watch_id},
                )
        return result.model_dump(mode="json")

    @router.post("/v1/assistant/pending/{pending_id}/answer")
    async def answer(pending_id: str, body: PendingAnswerBody, who: Caller) -> dict[str, Any]:
        """答复一个问题项；同一请求标识重试回原结果。"""
        item = await platform.butler.pending.answer(who, pending_id, body.text, body.request_id)
        return view(item)

    @router.get("/v1/assistant/actions/{request_id}")
    async def action_status(request_id: str, who: Caller) -> dict[str, Any]:
        """查询某轮自主办理动作的撤回状态（刷新后恢复按钮）。"""
        record = await platform.workspace.find_message(who, request_id + ":assistant")
        if record is None:
            record = await platform.workspace.find_message(who, request_id)
        actions = (record["metadata"].get("route", {}).get("executed_actions") or []) if record else []
        if not actions:
            raise NotFoundError("自主办理记录不存在")
        undone_ids: list[str] = []
        for item in actions:
            aid = str(item.get("action_id") or "")
            if aid and await platform.workspace.find_message(who, f"{request_id}:undo:{aid}"):
                undone_ids.append(aid)
        return {"request_id": request_id, "undone": undone_ids}

    @router.post("/v1/assistant/actions/{request_id}/{action_id}/undo")
    async def undo_action(request_id: str, action_id: str, who: Caller) -> dict[str, Any]:
        """一键撤回本轮管家自主办理的某一项个人操作（幂等、仅本人可用）。"""
        record = await platform.workspace.find_message(who, request_id + ":assistant")
        if record is None:
            record = await platform.workspace.find_message(who, request_id)
        actions = (record["metadata"].get("route", {}).get("executed_actions") or []) if record else []
        target = next((a for a in actions if a.get("action_id") == action_id), None)
        if target is None:
            raise NotFoundError("自主办理记录不存在")
        undo_key = f"{request_id}:undo:{action_id}"
        prior = await platform.workspace.find_message(who, undo_key)
        if prior is not None:
            return {
                "state": "undone",
                "request_id": request_id,
                "action_id": action_id,
                "result": prior["metadata"].get("result"),
            }
        result = await undo_executed_action(platform, who, target)
        await platform.butler.briefings.record_habit_action(
            who, str(target.get("action") or "personal"), undone=True,
        )
        label = str(target.get("title") or "操作")
        summary = str(target.get("summary") or "")
        note = f"已撤回：{label}（{summary}）" if summary else f"已撤回：{label}"
        await platform.workspace.append_message(
            who, "assistant", note, undo_key,
            metadata={"undo_of": request_id, "action_id": action_id, "result": result},
        )
        return {"state": "undone", "request_id": request_id, "action_id": action_id, "result": result}

    return router
