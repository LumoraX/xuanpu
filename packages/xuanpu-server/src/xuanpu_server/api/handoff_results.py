"""交接成果只接受有界原生上传，不能靠结果用途修改已有材料。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Query, Request
from pydantic import JsonValue, ValidationError  # noqa: TC002 —— FastAPI响应模型

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI依赖
from xuanpu_server.errors import InvalidRequestError
from xuanpu_server.handoffs import result_publication
from xuanpu_server.handoffs.schemas import (  # noqa: TC001 —— FastAPI请求模型
    ResultText,
    ResultUpload,
)

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


def handoff_results_router(platform: Platform) -> APIRouter:
    """共享双阶段发布最后仍重新检查当前交接资格、版本和源权限。"""
    router=APIRouter()
    service=platform.handoffs

    @router.post('/{identifier}/result-uploads',status_code=202)
    async def upload(identifier: str,request: Request,who: Caller,
                     request_id: Annotated[str,Query(min_length=1,max_length=160)],
                     expected_version: Annotated[int,Query(ge=1)],shared_version: Annotated[int,Query(ge=1)],
                     name: Annotated[str,Query(min_length=1,max_length=255)],confirmed: bool=False) -> dict[str,JsonValue]:
        """从流中限制总字节，不依赖来件自报大小。"""
        await service.shared.actor(who)
        if not confirmed:
            raise InvalidRequestError('发布交接成果需要明确确认')
        if set(request.query_params)-{'request_id','expected_version','shared_version','name','confirmed'}:
            raise InvalidRequestError('成果上传不能覆盖身份、材料或用途')
        try:
            body=ResultUpload(request_id=request_id,confirmed=True,expected_version=expected_version,shared_version=shared_version,name=name)
        except ValidationError as exc:
            raise InvalidRequestError('成果名称或确认字段无效') from exc
        chunks=bytearray()
        async for chunk in request.stream():
            if len(chunks)+len(chunk)>service.shared.config.max_material_bytes:
                raise InvalidRequestError('成果超过单文件上限')
            chunks.extend(chunk)
        mime=request.headers.get('content-type','application/octet-stream').split(';')[0].strip()
        return await result_publication.upload(service,who,identifier,body,bytes(chunks),mime)

    @router.get('/{identifier}/result-publications/{publication_id}')
    async def publication(identifier: str,publication_id: str,who: Caller) -> dict[str,JsonValue]:
        """仅查询本交接自己的发布状态。"""
        return await result_publication.publication(service,who,identifier,publication_id)

    @router.post('/{identifier}/result-texts',status_code=202)
    async def text(identifier: str,body: ResultText,who: Caller) -> dict[str,JsonValue]:
        """原生选定本人共享助手消息，正文与来源由服务器读取。"""
        return await result_publication.publish_text(platform,who,identifier,body)

    return router
