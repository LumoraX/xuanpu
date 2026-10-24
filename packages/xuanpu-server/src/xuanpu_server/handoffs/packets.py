"""承办人只获本次资料及成果权限，不能提升为原事项编辑者。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.ids import new_id
from xuanpu_server.shared_matters.models import SharedAccessGrant, SharedMember

if TYPE_CHECKING:
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.handoffs.models import Handoff


def grant_packet(tx: CoordinationTransaction, handoff: Handoff, user: str, now: float, *,
                 revisions: tuple[str, ...] | None = None, approval_id: str | None = None) -> str:
    """每次用途独立grant，发布成果不会改写已有阅读依赖的原资料包。"""
    grant_id, member_id = new_id('sgr'), new_id('smb')
    tx.shared.grants[grant_id] = SharedAccessGrant(tenant_id=tx.tenant_id, id=grant_id,
        matter_id=handoff.shared_id, subject_kind='user', subject_id=user, subject_epoch=tx.participant_epochs[user],
        scope='packet', packet_title=handoff.title, include_content=revisions is None and handoff.content_snapshot is not None,
        content_snapshot=handoff.content_snapshot if revisions is None else None,
        material_revisions=handoff.material_revision_ids if revisions is None else revisions,
        handoff_id=handoff.id, approval_id=approval_id, created_at=now, updated_at=now)
    tx.shared.members[member_id] = SharedMember(tenant_id=tx.tenant_id, id=member_id,
        matter_id=handoff.shared_id, grant_id=grant_id, subject_kind='user', subject_id=user, role='viewer',
        created_at=now, updated_at=now)
    return grant_id


def revoke_packets(tx: CoordinationTransaction, handoff_id: str, now: float, *, user: str | None = None,
                   approval_id: str | None = None) -> None:
    """精确撤销本用途，旧来源grant代次失效，不动其他独立协作授权。"""
    for identifier, grant in list(tx.shared.grants.items()):
        if grant.handoff_id != handoff_id or grant.status != 'active' or (user is not None and grant.subject_id != user):
            continue
        if approval_id is not None and grant.approval_id != approval_id:
            continue
        tx.shared.grants[identifier] = grant.model_copy(update={'status': 'revoked', 'epoch': grant.epoch + 1,
            'version': grant.version + 1, 'updated_at': now})
        for member_id, member in list(tx.shared.members.items()):
            if member.grant_id == identifier and member.status == 'active':
                tx.shared.members[member_id] = member.model_copy(update={'status': 'revoked', 'version': member.version + 1, 'updated_at': now})
