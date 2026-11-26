"""纯个人空间「人物与关系」HTTP 接口；鉴权与 CSRF 复用 Caller。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import Field

from server.api.auth import Caller  # noqa: TC001 — FastAPI 运行时解析
from server.core.errors import InvalidRequestError
from server.domains.identity.cookies import cookie_values, login_cookie_name
from server.domains.people.importer import MAX_IMPORT_BYTES, MAX_IMPORT_ITEMS
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


class ImportCommitBody(StrictModel):
    """确认导入联系人批次请求体。"""

    items: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_IMPORT_ITEMS)
    request_id: str = Field(min_length=1, max_length=128)


class GoogleConnectStartBody(StrictModel):
    """发起 Google 账号关联请求体。"""

    return_to: str = Field(default="/console/entry.html", max_length=2048)


class GooglePreviewBody(StrictModel):
    """拉取 Google 通讯录预览请求体。"""

    full_sync: bool = False


async def _read_bounded(request: Request, maximum: int, label: str = "头像") -> bytes:
    """流式有界读取上传字节，防止恶意超大请求耗尽内存。"""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum:
            raise InvalidRequestError(f"{label}不能超过 5 MiB")
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

    @router.post("/v1/people/import/preview")
    async def preview_import(
        request: Request,
        who: Caller,
        filename: Annotated[str, Query(max_length=255)] = "",
        fmt: Annotated[Literal["auto", "vcf", "csv"], Query(alias="format")] = "auto",
    ) -> dict[str, Any]:
        """上传 .vcf / .csv 通讯录文件并生成带查重提示的预览清单（不写入数据）。"""
        data = await _read_bounded(request, MAX_IMPORT_BYTES, label="导入文件")
        return await service.preview_import(who, data, filename=filename, fmt=fmt)

    @router.post("/v1/people/import", status_code=201)
    async def commit_import(body: ImportCommitBody, who: Caller) -> dict[str, Any]:
        """幂等提交已确认的通讯录导入批次（支持新建、合并更新与跳过）。"""
        return await service.commit_import(who, body.items, body.request_id)

    google = platform.google_contacts
    secure_cookie = platform.identity.settings.cookie_secure
    google_cookie_name = "__Host-xuanpu_google" if secure_cookie else "xuanpu_google"

    @router.get("/v1/people/google/status")
    async def google_status(who: Caller) -> dict[str, Any]:
        """读取本人 Google 通讯录关联状态与同步元数据（未配置时不发外网请求）。"""
        return await google.status(who)

    @router.post("/v1/people/google/connect/start")
    async def google_connect_start(
        who: Caller, body: GoogleConnectStartBody | None = None
    ) -> JSONResponse:
        """发起本人浏览器 Google OAuth2 + PKCE 授权，并种下五分钟一次性绑定 Cookie。"""
        target = body.return_to if body is not None else "/console/entry.html"
        outcome = await google.start_connect(who, return_to=target)
        response = JSONResponse(
            {"authorization_url": outcome["authorization_url"]},
            headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            google_cookie_name,
            outcome["cookie"],
            max_age=300,
            path="/",
            secure=secure_cookie,
            httponly=True,
            samesite="lax",
        )
        return response

    @router.get("/v1/people/google/callback")
    async def google_callback(
        request: Request,
        state: str = Query(default="", max_length=128),
        code: str = Query(default="", max_length=1024),
        error: str = Query(default="", max_length=128),
    ) -> RedirectResponse:
        """处理 Google OAuth 一次性回调，密文保存令牌并安全跳回站内页面。"""
        attempts = cookie_values(request.headers.getlist("cookie"), name=google_cookie_name)
        logins = cookie_values(
            request.headers.getlist("cookie"), name=login_cookie_name(secure_cookie)
        )
        attempt_cookie = attempts[0] if len(attempts) == 1 else None
        current_login = logins[0] if len(logins) == 1 else None
        outcome = await google.complete_callback(
            state=state,
            code=code,
            cookie=attempt_cookie,
            current_login=current_login,
            error=error,
        )
        response = RedirectResponse(
            outcome["return_to"],
            status_code=302,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
        response.delete_cookie(
            google_cookie_name,
            path="/",
            secure=secure_cookie,
            httponly=True,
            samesite="lax",
        )
        return response

    @router.post("/v1/people/google/preview")
    async def google_preview(
        who: Caller, body: GooglePreviewBody | None = None
    ) -> dict[str, Any]:
        """只读拉取已关联 Google 通讯录并与本地查重匹配，生成待确认预览。"""
        full_sync = body.full_sync if body is not None else False
        return await google.preview_sync(who, full_sync=full_sync)

    @router.post("/v1/people/google/sync", status_code=201)
    async def google_sync(body: dict[str, Any], who: Caller) -> dict[str, Any]:
        """提交已确认的 Google 同步批次或执行一键增量同步，并持久化外部映射与游标。"""
        return await google.commit_sync(who, body)

    @router.post("/v1/people/google/unlink")
    async def google_unlink(who: Caller) -> dict[str, Any]:
        """解除本人 Google 账号关联并清理密文令牌（保留已导入的本地联系人）。"""
        return await google.unlink(who)

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

    @router.get("/v1/people/orgs")
    async def list_orgs(
        who: Caller,
        q: str | None = None,
        city: str | None = None,
        tag: str | None = None,
        trashed: bool = False,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """按条件筛选本人的公司/组织列表。"""
        items = await service.list_orgs(who, q=q, city=city, tag=tag, trashed=trashed, limit=limit)
        return {"items": items}

    @router.post("/v1/people/orgs", status_code=201)
    async def create_org(body: CreatePersonBody, who: Caller) -> dict[str, Any]:
        """幂等创建纯个人空间公司/组织档案。"""
        return await service.create_org(who, body.profile, body.request_id)

    @router.get("/v1/people/orgs/{org_id}")
    async def get_org(org_id: str, who: Caller) -> dict[str, Any]:
        """读取公司/组织档案、跨实体关系与原始记录。"""
        return await service.get_org(who, org_id)

    @router.put("/v1/people/orgs/{org_id}")
    async def update_org(org_id: str, body: UpdatePersonBody, who: Caller) -> dict[str, Any]:
        """更新公司/组织档案并校验版本号。"""
        return await service.update_org(who, org_id, body.profile, body.version)

    @router.delete("/v1/people/orgs/{org_id}")
    async def trash_org(org_id: str, who: Caller, version: Annotated[int, Query(ge=1)]) -> dict[str, Any]:
        """软删除公司/组织档案。"""
        return await service.trash_org(who, org_id, version)

    @router.post("/v1/people/orgs/{org_id}/restore")
    async def restore_org(org_id: str, who: Caller, version: Annotated[int, Query(ge=1)]) -> dict[str, Any]:
        """恢复公司/组织档案。"""
        return await service.restore_org(who, org_id, version)

    @router.get("/v1/people/graph")
    async def full_graph(
        who: Caller,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        include_isolated: bool = False,
    ) -> dict[str, Any]:
        """读取本人个人空间全景关系图谱（支持有界节点上限与是否包含孤立实体）。"""
        return await service.full_graph(who, limit=limit, include_isolated=include_isolated)

    @router.get("/v1/people/graph/{entity_kind}/{entity_id}")
    async def entity_graph(
        entity_kind: str,
        entity_id: str,
        who: Caller,
        depth: Annotated[int, Query(ge=1, le=3)] = 2,
    ) -> dict[str, Any]:
        """跨实体 1–3 跳个人知识图谱查询（支持 person / org / matter / record）。"""
        return await service.entity_graph(who, entity_kind, entity_id, depth=depth)

    @router.get("/v1/people/notes")
    @router.get("/v1/people/records")
    async def list_notes(
        who: Caller,
        person_id: str | None = None,
        org_id: str | None = None,
        matter_id: str | None = None,
        record_kind: str | None = None,
        location: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """检索本人原始记录（随手记、会议纪要、名片与通话）。"""
        items = await service.list_notes(
            who,
            person_id=person_id,
            org_id=org_id,
            matter_id=matter_id,
            record_kind=record_kind,
            location=location,
            since=since,
            until=until,
            q=q,
            limit=limit,
        )
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
