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
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析
from xuanpu_server.butler.pending import view
from xuanpu_server.butler.service import ButlerMessageBody  # noqa: TC001 —— FastAPI 运行时解析

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from xuanpu_server.platform import Platform


def _frame(name: str, data: dict[str, Any]) -> str:
    """一帧 SSE。"""
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class PendingAnswerBody(BaseModel):
    """员工对一个待处理问题的答复。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.:-]+$")
    text: str = Field(min_length=1, max_length=2000, pattern=r"\S")


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
    async def artisans(who: Caller) -> dict[str, Any]:
        """本人此刻可用的匠人（包名、入口、说明）；只给当前启用的员工。"""
        await platform.workspace.require_employee(who)
        found = await platform.artisans.available(who)
        return {"items": [{"package": a.ref.package, "entry": a.ref.entry, "description": a.description,
                           "host": a.host} for a in found]}

    @router.get("/v1/assistant/pending")
    async def pending(who: Caller) -> dict[str, Any]:
        """本人仍待处理的项（不含已答复、已解决与代答记录）。"""
        return {"items": [view(i) for i in await platform.butler.pending.list(who)]}

    @router.post("/v1/assistant/pending/{pending_id}/answer")
    async def answer(pending_id: str, body: PendingAnswerBody, who: Caller) -> dict[str, Any]:
        """答复一个问题项；同一请求标识重试回原结果。"""
        item = await platform.butler.pending.answer(who, pending_id, body.text, body.request_id)
        return view(item)

    return router
