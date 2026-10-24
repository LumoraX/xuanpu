"""原生动作由服务器当前授权投影；按钮不代替正式接口复核。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import NotFoundError
from xuanpu_server.shared_matters.permissions import access

if TYPE_CHECKING:
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.shared_matters.models import SharedAccessGrant, SharedMatter
    from xuanpu_server.shared_matters.service import SharedMatterService
    from xuanpu_server.tenancy import Principal


async def allowed_actions(service: SharedMatterService, tx: CoordinationTransaction, who: Principal,
                          matter: SharedMatter, grants: list[SharedAccessGrant]) -> list[str]:
    """普通viewer、负责人、editor及灰度关闭时均有准确可用动作。"""
    actions = {'read_materials'}
    full = any(grant.scope == 'full' for grant in grants)
    if full:
        actions.update(('read_members', 'read_history'))
    if service.config.allows(tx.tenant_id):
        actions.add('start_context')
    owner = await service.identity.get_user(tx.tenant_id, matter.owner_id)
    if owner is None or owner.status != 'active':
        return sorted(actions)
    try:
        access(tx, who, matter.id, service.now(), write=True)
    except NotFoundError:
        return sorted(actions)
    actions.add('archive')
    if service.config.allows(tx.tenant_id):
        actions.update(('edit', 'publish_material'))
    if matter.owner_id == who.user_id:
        actions.add('revoke_member')
        if service.config.allows(tx.tenant_id):
            actions.update(('manage_members', 'transfer_owner'))
    return sorted(actions)
