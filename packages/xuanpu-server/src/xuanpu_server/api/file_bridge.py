"""外部集成的只读身份和本人固定版本文件桥，复用平台认证与工作台权限。"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Annotated
from urllib.parse import quote

from fastapi import APIRouter, Query, Response

from xuanpu_server.api.auth import Caller  # noqa: TC001 — FastAPI运行时解析
from xuanpu_server.errors import InvalidRequestError

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


def file_bridge_router(platform: Platform) -> APIRouter:
    """仅暴露已认证的真实员工身份和其本人不可变文件版本。"""
    router = APIRouter(prefix="/v1/integration-gateway")
    service = platform.workspace

    @router.get("/identity")
    async def identity(who: Caller) -> dict[str, str | bool]:
        """身份只能来自受信任的 API key 代员工调用或普通登录 cookie。"""
        tenant_id, user_id = await service.require_employee(who)
        return {"tenant_id": tenant_id, "user_id": user_id, "active": True}

    @router.get("/files/{item_id}")
    async def download(
        item_id: str,
        who: Caller,
        revision: Annotated[int, Query(ge=1)],
        sha256: Annotated[str, Query(pattern=r"^[0-9a-fA-F]{64}$")],
    ) -> Response:
        """限定本人文件与明确版本，读取后复核大小和调用方声明的摘要。"""
        await service.require_employee(who)
        body, mime, name = await service.download(who, item_id, revision)
        if len(body) > service.max_file_bytes:
            raise InvalidRequestError("文件正本超过大小限制")
        if hashlib.sha256(body).hexdigest() != sha256.lower():
            raise InvalidRequestError("文件版本摘要不匹配")
        return Response(
            body,
            media_type=mime,
            headers={
                "Content-Disposition": "attachment; filename=download; filename*=UTF-8''"
                + quote(name, safe=""),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": "sandbox",
            },
        )

    return router
