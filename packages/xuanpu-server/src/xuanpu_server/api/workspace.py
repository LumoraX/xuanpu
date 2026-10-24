"""原生个人工作台 HTTP 接口；认证和 CSRF 复用 Caller。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Query, Request, Response
from pydantic import Field

from xuanpu_server.api.auth import Caller  # noqa: TC001 — FastAPI运行时解析
from xuanpu_server.errors import InvalidRequestError
from xuanpu_server.workspace.models import SettingsData, StrictModel

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


class CreateBody(StrictModel):
    """领域模型在服务层按 kind 再做严格校验。"""

    kind: str
    data: dict[str, Any]
    request_id: str = Field(min_length=1, max_length=128)


class UpdateBody(StrictModel):
    """修改携带明确版本，仅接受该类型的可编辑字段。"""

    data: dict[str, Any]
    version: int = Field(ge=1)


class SettingsBody(SettingsData):
    """个人偏好更新必须提供当前版本。"""

    version: int = Field(ge=1)


async def content(request: Request, maximum: int) -> tuple[bytes, str]:
    """有界流式读取，防止请求先整体载入内存。"""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise InvalidRequestError("单个文件不能超过20 MiB")
        chunks.append(chunk)
    mime = (
        request.headers.get("content-type", "application/octet-stream")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    return b"".join(chunks), mime


def workspace_router(platform: Platform) -> APIRouter:
    """路由仅翻译请求，不复制领域授权和校验规则。"""
    router = APIRouter(prefix="/v1/workspace")
    service = platform.workspace

    @router.get("/items")
    async def list_items(
        who: Caller, kind: str | None = None, trashed: bool = False, search: str | None = None
    ) -> dict[str, Any]:
        """列本人工作台记录。"""
        return {"items": await service.list(who, kind=kind, trashed=trashed, search=search)}

    @router.post("/items")
    async def create_item(body: CreateBody, who: Caller) -> dict[str, Any]:
        """创建本人记录。"""
        return await service.create(who, body.kind, body.data, body.request_id)

    @router.get("/items/{item_id}")
    async def get_item(item_id: str, who: Caller) -> dict[str, Any]:
        """读取单条本人记录。"""
        return await service.get(who, item_id)

    @router.put("/items/{item_id}")
    async def update_item(item_id: str, body: UpdateBody, who: Caller) -> dict[str, Any]:
        """修改记录并检查版本。"""
        return await service.update(who, item_id, body.data, body.version)

    @router.delete("/items/{item_id}")
    async def trash_item(
        item_id: str, who: Caller, version: Annotated[int, Query(ge=1)]
    ) -> dict[str, Any]:
        """递归移入本人回收站。"""
        return await service.trash(who, item_id, version)

    @router.post("/items/{item_id}/restore")
    async def restore_item(
        item_id: str, who: Caller, version: Annotated[int, Query(ge=1)]
    ) -> dict[str, Any]:
        """恢复本人回收批次。"""
        return await service.restore(who, item_id, version)

    @router.post("/files")
    async def upload(
        request: Request, who: Caller, name: str, request_id: str, parent_id: str | None = None
    ) -> dict[str, Any]:
        """上传文件正本，字节来自原始请求体。"""
        await service.require_employee(who)
        body, mime = await content(request, service.max_file_bytes)
        return await service.upload(who, name, body, mime, request_id, parent_id)

    @router.put("/files/{item_id}/content")
    async def replace_content(
        item_id: str,
        request: Request,
        who: Caller,
        version: Annotated[int, Query(ge=1)],
        request_id: str,
    ) -> dict[str, Any]:
        """增加文件新版本。"""
        await service.require_employee(who)
        body, mime = await content(request, service.max_file_bytes)
        return await service.replace_content(who, item_id, body, mime, version, request_id)

    @router.get("/files/{item_id}/content")
    async def download(
        item_id: str, who: Caller, revision: Annotated[int | None, Query(ge=1)] = None
    ) -> Response:
        """安全下载，不让浏览器以内联文档执行文件内容。"""
        body, mime, name = await service.download(who, item_id, revision)
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

    @router.get("/files/{item_id}/versions")
    async def versions(item_id: str, who: Caller) -> dict[str, Any]:
        """列出本人文件不可变版本。"""
        return {"items": await service.versions(who, item_id)}

    @router.get("/settings")
    async def settings(who: Caller) -> dict[str, Any]:
        """读取本人助手偏好。"""
        return await service.settings(who)

    @router.put("/settings")
    async def update_settings(body: SettingsBody, who: Caller) -> dict[str, Any]:
        """保存助手偏好并校验版本。"""
        return await service.update_settings(
            who, body.model_dump(exclude={"version"}), body.version
        )

    @router.get("/summary")
    async def summary(who: Caller) -> dict[str, Any]:
        """读取本人工作概览与文件空间。"""
        return await service.summary(who)

    @router.get("/messages")
    async def messages(
        who: Caller,
        after_id: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        latest: bool = False,
        matter_id: str | None = None,
        before_id: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
        private_only: bool = False,
        source_context_id: Annotated[str | None, Query(min_length=1, max_length=160)] = None,
    ) -> dict[str, Any]:
        """按持久游标读取主对话；写入由助手路由统一管理。"""
        return {
            "items": await service.messages(
                who, after_id, limit, latest=latest, matter_id=matter_id, before_id=before_id,
                private_only=private_only, source_context_id=source_context_id,
            )
        }

    return router
