"""管家入口：``POST /v1/assistant/messages`` 与待本人处理项。

开启管家的租户由管家处理（自办或派给一位匠人，不等匠人结果）；其余租户走既有分流。
``request_id`` 必填：同键同体回原回执，不同体 409。

待本人处理项（R5-C2）：``GET /v1/assistant/pending`` 列出本人仍待处理的问题与审批提示；
``POST /v1/assistant/pending/{id}/answer`` 答复问题，原话转给匠人。审批类项请在既有待确认里处理。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析
from xuanpu_server.butler.pending import view
from xuanpu_server.butler.service import ButlerMessageBody  # noqa: TC001 —— FastAPI 运行时解析

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


class PendingAnswerBody(BaseModel):
    """员工对一个待处理问题的答复。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.:-]+$")
    text: str = Field(min_length=1, max_length=2000, pattern=r"\S")


def butler_router(platform: Platform) -> APIRouter:
    """注册管家入口与待处理项接口。"""
    router = APIRouter()

    @router.post("/v1/assistant/messages")
    async def message(body: ButlerMessageBody, who: Caller) -> dict[str, Any]:
        """员工对管家说一句话；返回回执（``mode`` 为 butler 或 route）。"""
        return await platform.butler.message(who, body)

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
