"""固定共享材料的原生发布、下载与撤回接口。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated
from urllib.parse import quote

from fastapi import APIRouter, Query, Request, Response
from pydantic import JsonValue  # noqa: TC002 —— FastAPI响应类型

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI依赖
from xuanpu_server.errors import InvalidRequestError
from xuanpu_server.shared_matters.schemas import (  # noqa: TC001 —— FastAPI请求模型
    PublishMaterialInput,
    PublishTextDraft,
    SharedChange,
)

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


def shared_materials_router(platform: Platform) -> APIRouter:
    """每个请求独立重新检查当前员工和精确材料版本权限。"""
    router = APIRouter()
    service = platform.shared_matters

    @router.post('/{identifier}/material-texts', status_code=202)
    async def text_draft(identifier: str, body: PublishTextDraft, who: Caller) -> dict[str, JsonValue]:
        """原生确认发布本人受保护文本，保留固定来源而不另存私人文件。"""
        from xuanpu_server.shared_matters.text_drafts import publish_text
        return await publish_text(platform, who, identifier, body)

    @router.post('/{identifier}/materials', status_code=202)
    async def publish(identifier: str, body: PublishMaterialInput, who: Caller) -> dict[str, JsonValue]:
        """明确发布本人文件的固定版本，结果通过意图状态补读。"""
        return await platform.shared_materials.publish(who, identifier, body)

    @router.post('/{identifier}/material-uploads', status_code=202)
    async def upload(identifier: str, request: Request, who: Caller,
                     request_id: Annotated[str, Query(min_length=1, max_length=160)],
                     expected_version: Annotated[int, Query(ge=1)],
                     name: Annotated[str, Query(min_length=1, max_length=255)],
                     confirmed: bool = False) -> dict[str, JsonValue]:
        """上传在读取时限制总字节，不信任Content-Length。"""
        await service.actor(who)
        if not confirmed:
            raise InvalidRequestError('发布共享副本需要明确确认')
        chunks = bytearray()
        async for chunk in request.stream():
            if len(chunks) + len(chunk) > service.config.max_material_bytes:
                raise InvalidRequestError('共享材料超过单文件上限')
            chunks.extend(chunk)
        mime = request.headers.get('content-type', 'application/octet-stream').split(';')[0].strip()
        return await platform.shared_materials.upload(who, identifier, request_id=request_id,
            expected=expected_version, name=name, mime=mime, data=bytes(chunks))

    @router.get('/{identifier}/publications/{publication_id}')
    async def publication(identifier: str, publication_id: str, who: Caller) -> dict[str, JsonValue]:
        """获取当前授权内的发布状态，不返回对象路径。"""
        return await platform.shared_materials.publication(who, identifier, publication_id)

    @router.get('/{identifier}/materials')
    async def materials(identifier: str, who: Caller, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """仅枚举当前有权读取的固定材料版本。"""
        return await platform.shared_materials.listing(who, identifier, limit=limit, cursor=cursor)

    @router.get('/{identifier}/materials/{material_id}/content')
    async def download(identifier: str, material_id: str, request: Request, who: Caller,
                       revision: Annotated[int, Query(ge=1)]) -> Response:
        """每次下载重新授权，关闭缓存及字节范围续传。"""
        if 'range' in request.headers:
            raise InvalidRequestError('请重新请求完整的授权材料版本')
        data, mime, name, sha = await platform.shared_materials.download(who, identifier, material_id, revision)
        return Response(data, media_type=mime, headers={'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff', 'X-Content-SHA256': sha,
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(name, safe='')})

    @router.post('/{identifier}/materials/{material_id}/withdraw')
    async def withdraw(identifier: str, material_id: str, body: SharedChange, who: Caller) -> dict[str, JsonValue]:
        """原件保留与共享撤回分别操作。"""
        return await platform.shared_materials.withdraw(who, identifier, material_id, body)

    return router
