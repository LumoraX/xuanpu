"""企微账号与已有稳定usr的明确映射。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_server.connections.rules import get
from xuanpu_server.errors import NotFoundError
from xuanpu_server.identity.models import ExternalIdentity

if TYPE_CHECKING:
    from xuanpu_server.connections.models import AccountBindingInput
    from xuanpu_server.connections.service import ConnectionService
    from xuanpu_server.tenancy import Principal


def subject(corp: str,userid: str) -> str:
    """固定企业与不区分大小写的成员ID，不使用显示名。"""
    return json.dumps([corp,userid.lower()],separators=(',',':'))


async def bind(service: ConnectionService,who: Principal,cid: str,
               body: AccountBindingInput) -> dict[str,Any]:
    """管理员预配账号，不授管理员角色，不改变本地姓名。"""
    tenant,_=await service.actor(who,admin=True)
    async with service.store.transaction(tenant) as state:
        install=dict(get(state,cid,'installation'))
    user=await service.identity.get_user(tenant,body.user_id)
    if user is None or user.status!='active':
        raise NotFoundError('员工不存在或已停用')
    identity=ExternalIdentity(tenant_id=tenant,provider='wecom',
        subject=subject(install['corp_id'],body.userid),user_id=body.user_id,
        attributes={'corp_id':install['corp_id'],'userid':body.userid})
    existing=await service.identity.get_user_by_identity(tenant,'wecom',identity.subject)
    if existing is None or existing.user_id!=user.user_id:
        await service.identity.create_external_identity(identity)
    return {'user_id':user.user_id,'provider':'wecom','bound':True}
