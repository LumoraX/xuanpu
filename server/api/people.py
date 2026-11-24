"""纯个人空间「人物与关系」HTTP 接口；鉴权与 CSRF 复用 Caller。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Query, Request, Response
from pydantic import Field

from server.api.auth import Caller  # noqa: TC001 — FastAPI 运行时解析
from server.core.errors import InvalidRequestError
from server.domains.people.models import (
    MAX_AVATAR_BYTES,
    MergeInput,
    StrictModel,
    UnmergeInput,
)

if TYPE_CHECKING:
    from server.platform import Platform


class CreatePersonBody(StrictModel):
    """新建人物请求体，profile 在领域服务内按 PersonProfile 严格校验。"""

    profile: dict[str, Any]
    request_id: str = Field(min_length=1, max_length=128)


class UpdatePersonBody(StrictModel):
    """更新人物资料须提供当前版本。"""

    profile: dict[str, Any]
    version: int = Field(ge=1)


class CreateRelationBody(StrictModel):
    """新建关系边请求体。"""

    data: dict[str, Any]
    request_id: str = Field(min_length=1, max_length=128)


class UpdateRelationBody(StrictModel):
    """更新关系边属性须提供当前版本。"""

    data: dict[str, Any]
    version: int = Field(ge=1)


class CreateNoteBody(StrictModel):
    """新建原始互动记录请求体。"""

    data: dict[str, Any]
    request_id: str = Field(min_length=1, max_length=128)


class BundleBody(StrictModel):
    """一次提交多个人物、关系与互动笔记的复合请求体。"""

    bundle: dict[str, Any]
    request_id: str = Field(min_length=1, max_length=128)


class CandidatesBody(StrictModel):
    """重名/同联系方式候选查询请求体。"""

    profile: dict[str, Any]


async def _read_bounded(request: Request, maximum: int) -> bytes:
    """流式有界读取头像字节，防止恶意超大请求耗尽内存。"""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum:
            raise InvalidRequestError("头像不能超过 5 MiB")
        chunks.append(chunk)
    return b"".join(chunks)


def people_router(platform: Platform) -> APIRouter:
    """路由只做请求翻译，全部授权、隔离与不变量由 PeopleService 承担。"""
    router = APIRouter()
    service = platform.people

    @router.get("/v1/people")
    async def list_people(
        who: Caller,
        q: str | None = None,
        tag: str | None = None,
        organization: str | None = None,
        birth_month: int | None = None,
        matter_id: str | None = None,
        trashed: bool = False,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """按姓名、别名、公司、标签、生日月份或事项筛选本人的人物档案。"""
        items = await service.list_people(
            who,
            q=q,
            tag=tag,
            organization=organization,
            birth_month=birth_month,
            matter_id=matter_id,
            trashed=trashed,
            limit=limit,
        )
        return {"items": items}

    @router.post("/v1/people", status_code=201)
    async def create_person(body: CreatePersonBody, who: Caller) -> dict[str, Any]:
        """新建本人人物档案。"""
        return await service.create_person(who, body.profile, body.request_id)

    @router.post("/v1/people/candidates")
    async def candidates(body: CandidatesBody, who: Caller) -> dict[str, Any]:
        """检索疑似重复的已有人物候选。"""
        return {"items": await service.find_candidates(who, body.profile)}

    @router.post("/v1/people/bundle", status_code=201)
    async def apply_bundle(body: BundleBody, who: Caller) -> dict[str, Any]:
        """原子提交人物、关系与原始笔记组合。"""
        return await service.apply_bundle(who, body.bundle, body.request_id)

    @router.post("/v1/people/relations", status_code=201)
    async def create_relation(body: CreateRelationBody, who: Caller) -> dict[str, Any]:
        """新建人↔人或人↔事项关系边。"""
        return await service.create_relation(who, body.data, body.request_id)

    @router.put("/v1/people/relations/{relation_id}")
    async def update_relation(relation_id: str, body: UpdateRelationBody, who: Caller) -> dict[str, Any]:
        """带版本校验更新关系边。"""
        return await service.update_relation(who, relation_id, body.data, body.version)

    @router.delete("/v1/people/relations/{relation_id}")
    async def delete_relation(
        relation_id: str, who: Caller, version: Annotated[int, Query(ge=1)]
    ) -> dict[str, Any]:
        """软删除关系边。"""
        return await service.delete_relation(who, relation_id, version)

    @router.get("/v1/people/notes")
    async def list_notes(
        who: Caller,
        person_id: str | None = None,
        matter_id: str | None = None,
        q: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """检索本人原始互动记录。"""
        items = await service.list_notes(who, person_id=person_id, matter_id=matter_id, q=q, limit=limit)
        return {"items": items}

    @router.post("/v1/people/notes", status_code=201)
    async def create_note(body: CreateNoteBody, who: Caller) -> dict[str, Any]:
        """保存一条原始互动或名片文本记录。"""
        return await service.create_note(who, body.data, body.request_id)

    @router.delete("/v1/people/notes/{note_id}")
    async def delete_note(note_id: str, who: Caller, version: Annotated[int, Query(ge=1)]) -> dict[str, Any]:
        """软删除一条互动记录。"""
        return await service.delete_note(who, note_id, version)

    @router.get("/v1/people/{person_id}")
    async def get_person(person_id: str, who: Caller, include_history: bool = False) -> dict[str, Any]:
        """读取人物详情、关联关系与最近互动记录。"""
        return await service.get_person(who, person_id, include_history=include_history)

    @router.put("/v1/people/{person_id}")
    async def update_person(person_id: str, body: UpdatePersonBody, who: Caller) -> dict[str, Any]:
        """更新人物档案并检查版本。"""
        return await service.update_person(who, person_id, body.profile, body.version)

    @router.delete("/v1/people/{person_id}")
    async def trash_person(person_id: str, who: Caller, version: Annotated[int, Query(ge=1)]) -> dict[str, Any]:
        """将人物与关联活动关系移入回收站。"""
        return await service.trash_person(who, person_id, version)

    @router.post("/v1/people/{person_id}/restore")
    async def restore_person(person_id: str, who: Caller, version: Annotated[int, Query(ge=1)]) -> dict[str, Any]:
        """从回收站恢复人物。"""
        return await service.restore_person(who, person_id, version)

    @router.post("/v1/people/{person_id}/merge")
    async def merge_person(person_id: str, body: MergeInput, who: Caller) -> dict[str, Any]:
        """将源人物软合并到目标人物。"""
        return await service.merge_people(who, person_id, body)

    @router.post("/v1/people/{person_id}/unmerge")
    async def unmerge_person(person_id: str, body: UnmergeInput, who: Caller) -> dict[str, Any]:
        """撤销一次已执行的软合并。"""
        return await service.unmerge_people(who, person_id, body)

    @router.get("/v1/people/{person_id}/graph")
    async def person_graph(
        person_id: str,
        who: Caller,
        depth: Annotated[int, Query(ge=1, le=2)] = 2,
        include_matters: bool = True,
    ) -> dict[str, Any]:
        """展开 1–2 跳人脉与事项关系图。"""
        return await service.graph(who, person_id, depth=depth, include_matters=include_matters)

    @router.put("/v1/people/{person_id}/avatar")
    async def upload_avatar(
        person_id: str, request: Request, who: Caller, version: Annotated[int, Query(ge=1)]
    ) -> dict[str, Any]:
        """上传人物头像（仅支持 JPEG/PNG/WebP，最大 5 MiB）。"""
        data = await _read_bounded(request, MAX_AVATAR_BYTES)
        return await service.upload_avatar(who, person_id, data, version)

    @router.get("/v1/people/{person_id}/avatar")
    async def download_avatar(person_id: str, who: Caller) -> Response:
        """本人受权读取人物头像，禁止内容嗅探与公开缓存。"""
        data, mime = await service.get_avatar(who, person_id)
        return Response(
            content=data,
            media_type=mime,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    @router.delete("/v1/people/{person_id}/avatar")
    async def remove_avatar(person_id: str, who: Caller, version: Annotated[int, Query(ge=1)]) -> dict[str, Any]:
        """移除人物头像。"""
        return await service.delete_avatar(who, person_id, version)

    @router.get("/v1/workspace/items/{matter_id}/people")
    async def matter_people(matter_id: str, who: Caller) -> dict[str, Any]:
        """按本人事项反查关联人物与角色。"""
        return {"items": await service.list_matter_people(who, matter_id)}

    return router
