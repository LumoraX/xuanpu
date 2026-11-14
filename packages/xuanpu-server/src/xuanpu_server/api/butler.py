"""管家入口：``POST /v1/assistant/messages``。

开启管家的租户由管家处理（自办或派给一位匠人，不等匠人结果）；其余租户走既有分流。
``request_id`` 必填：同键同体回原回执，不同体 409。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析
from xuanpu_server.butler.service import ButlerMessageBody  # noqa: TC001 —— FastAPI 运行时解析

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


def butler_router(platform: Platform) -> APIRouter:
    """注册管家入口。"""
    router = APIRouter()

    @router.post("/v1/assistant/messages")
    async def message(body: ButlerMessageBody, who: Caller) -> dict[str, Any]:
        """员工对管家说一句话；返回回执（``mode`` 为 butler 或 route）。"""
        return await platform.butler.message(who, body)

    return router
