"""员工直接输入需求的业务分流 HTTP 入口。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, Response

from xuanpu_server.api import schemas
from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析
from xuanpu_server.assistant import RouteBody  # noqa: TC001 —— FastAPI 运行时解析

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


def assistant_router(platform: Platform) -> APIRouter:
    """只转交给平台分流服务，不绕过会话权限和配额。"""
    router = APIRouter()

    @router.post("/v1/assistant/route", response_model=None)
    async def route(body: RouteBody, who: Caller, request: Request) -> dict[str, Any] | Response:
        """选择已开通业务、提出澄清，或说明未开通。"""
        work = asyncio.create_task(platform.assistant.route(who, body))
        disconnected = asyncio.create_task(_wait_disconnect(request))
        try:
            done, _ = await asyncio.wait((work, disconnected), return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                return Response(status_code=499)
            result = work.result()
            return {
                "session": schemas.session_view(result.session) if result.session else None,
                "message": result.message,
                "reminder_draft": result.reminder_draft,
                "open_reminders": result.open_reminders,
                "workspace_draft": result.workspace_draft,
                "open_workspace": result.open_workspace,
                "proposal_id": result.proposal_id,
                "proactive_draft": result.proactive_draft,
                "open_proactive": result.open_proactive,
                "work_task_id": result.work_task_id,
                "preserve_input": result.preserve_input,
                "task_definition_draft": result.task_definition_draft,
                "open_task_definitions": result.open_task_definitions,
                "open_connections": result.open_connections,
                "shared_draft": result.shared_draft, "open_shared": result.open_shared,
            }
        finally:
            # 普通HTTP请求的断连不会自动取消handler；显式取消采样并等待有界计量收尾。
            if not work.done():
                work.cancel()
            disconnected.cancel()
            await asyncio.gather(work, disconnected, return_exceptions=True)

    return router


async def _wait_disconnect(request: Request) -> None:
    """请求体已由FastAPI读完；等待传输层的断连事件，不轮询或抢读业务正文。"""
    while (await request.receive())["type"] != "http.disconnect":
        continue
