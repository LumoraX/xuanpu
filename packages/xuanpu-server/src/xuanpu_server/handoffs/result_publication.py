"""本交接新成果的受限发布沿用共享副本两阶段流程，不提升编辑权限。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, NotFoundError, PermissionDeniedError
from xuanpu_server.handoffs.permissions import assignee_current, source_valid
from xuanpu_server.shared_matters.permissions import access
from xuanpu_server.shared_matters.views import selection_version

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.handoffs.schemas import ResultText, ResultUpload
    from xuanpu_server.handoffs.service import HandoffService
    from xuanpu_server.platform import Platform
    from xuanpu_server.shared_matters.materials import SharedMaterials
    from xuanpu_server.shared_matters.models import SharedMatter
    from xuanpu_server.tenancy import Principal


async def require_result(tx: CoordinationTransaction, materials: SharedMaterials, who: Principal,
                         shared_id: str, handoff_id: str, *, version: int | None = None,
                         shared_version: int | None = None, projected: bool = False) -> SharedMatter:
    """先查用途身份；新写入还要当前处理状态及两个明确CAS。"""
    who=await materials.service.actor(who)
    handoff=tx.collaboration.handoffs.get(handoff_id)
    if (handoff is None or handoff.shared_id!=shared_id or who.user_id!=handoff.assignee_id
            or not assignee_current(tx,handoff,who.user_id or '',materials.service.now())):
        raise PermissionDeniedError('只有当前承办人可以发布本次新成果')
    if handoff.blocked_reason is not None or not await source_valid(tx,handoff,materials):
        raise ConflictError('交接原来源已失效')
    matter=tx.shared.matters[shared_id]
    if version is not None and (handoff.version!=version or handoff.status!='in_progress' or handoff.cancel_requested):
        raise ConflictError('交接版本或状态已变化，请重新核对')
    matter,grants=access(tx,who,shared_id,materials.service.now())
    visible_version=selection_version(matter,grants) if projected else matter.version
    if shared_version is not None and (visible_version!=shared_version or matter.status!='active'):
        raise ConflictError('共享事项版本或状态已变化，请重新核对')
    return matter


async def upload(service: HandoffService, who: Principal, identifier: str, body: ResultUpload,
                 data: bytes, mime: str) -> dict[str, JsonValue]:
    """只有新材料可被本用途创建，不能传入原材料ID覆盖输入。"""
    who=await service.shared.actor(who)
    async with service.transaction(who,identifier) as (_tx,handoff):
        shared_id=handoff.shared_id
    publications=service.materials.publications
    result=await publications.prepare(who,shared_id,request_id=body.request_id,expected=body.shared_version,
        data=data,name=body.name,mime=mime,result_for_handoff=identifier,handoff_version=body.expected_version)
    await publications.execute(who.require_tenant(),str(result['id']),data=data)
    return result


async def publication(service: HandoffService, who: Principal, identifier: str, publication_id: str) -> dict[str,JsonValue]:
    """允许当前参与人补读发布结果状态，永不返回对象路径或正文。"""
    who=await service.shared.actor(who)
    async with service.transaction(who,identifier) as (tx,handoff):
        if who.user_id not in (handoff.assignee_id,handoff.requester_id):
            raise NotFoundError('成果发布记录不存在')
        current=tx.shared.publications.get(publication_id)
        if current is None or current.result_for_handoff!=identifier:
            raise NotFoundError('成果发布记录不存在')
        return {k:v for k,v in current.model_dump(mode='json').items()
            if k in ('id','material_id','revision','version','status','error_code')}


async def publish_text(platform: Platform,who: Principal,identifier: str,body: ResultText) -> dict[str,JsonValue]:
    """本人共享文本直接成为固定成果，保留来源，不另建私有文件或信任客户端正文。"""
    from xuanpu_server.shared_matters.text_drafts import selected_draft
    who=await platform.handoffs.shared.actor(who)
    async with platform.handoffs.transaction(who,identifier) as (_tx,handoff):
        shared_id=handoff.shared_id
    data,refs=await selected_draft(platform,who,body.source_context_id,body.draft_message_id)
    publications=platform.shared_materials.publications
    result=await publications.prepare(who,shared_id,request_id=body.request_id,expected=body.shared_version,
        name=body.name,mime='text/plain',data=data,source_refs=refs,draft_message_id=body.draft_message_id,
        result_for_handoff=identifier,handoff_version=body.expected_version)
    await publications.execute(who.require_tenant(),str(result['id']),data=data)
    return result
