"""R2只保存固定授权引用；恢复不自动领取新同意。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.connections.rules import get
from xuanpu_server.errors import ConflictError, NotFoundError, PermissionDeniedError

if TYPE_CHECKING:
    from xuanpu_server.connections.models import ConnectionRef
    from xuanpu_server.connections.service import ConnectionService
    from xuanpu_server.tenancy import Principal


async def snapshot(service: ConnectionService,who: Principal,digest: str) -> tuple[ConnectionRef,...]:
    """按当前不可变包绑定冻结引用，不冻结token。"""
    tenant=who.require_tenant()
    async with service.store.transaction(tenant) as pending:
        found=any(r.get('kind')=='package_binding' and r['package_digest']==digest for r in pending.records.values())
    if not found:
        return ()
    tenant,user=await service.actor(who)
    refs: dict[tuple[str,str],ConnectionRef]={}
    async with service.store.transaction(tenant) as state:
        for record in state.records.values():
            if record.get('kind')!='package_binding' or record['package_digest']!=digest:
                continue
            operation=record['operation']
            if operation=='directory_preview.read':
                await service.actor(who,admin=True)
            cid=record['installation_id']
            _,ref=service.reference(state,cid,user,operation)
            refs[(cid,ref.consent_id)]=ref
    return tuple(refs.values())


async def check_refs(service: ConnectionService,who: Principal,refs: tuple[ConnectionRef,...]) -> None:
    """引用失效给出固定阻塞码，旧批准不能跳过。"""
    if not refs:
        return
    try:
        tenant,user=await service.actor(who)
        async with service.store.transaction(tenant) as state:
            for ref in refs:
                install=get(state,ref.installation_id,'installation')
                service.active(install)
                consent=get(state,ref.consent_id,'consent')
                service.valid_consent(consent,install,user)
                if 'directory_preview.read' in consent['operations']:
                    employee=await service.identity.get_user(tenant,user)
                    if employee is None or employee.role!='tenant_admin':
                        raise PermissionDeniedError('当前管理员权限已失效')
                if (ref.installation_version!=install['version'] or ref.consent_epoch!=consent['epoch']
                    or consent['installation_id']!=ref.installation_id):
                    raise PermissionDeniedError('旧授权引用已变化')
    except (PermissionDeniedError,ConflictError,NotFoundError) as exc:
        raise PermissionDeniedError('connection_reauthorization_required') from exc
