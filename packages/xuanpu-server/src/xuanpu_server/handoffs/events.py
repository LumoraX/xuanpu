"""责任历史、协调事件与通用站内动态在同一事务追加。"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from xuanpu_server.collaboration.models import CollaborationNotification
from xuanpu_server.handoffs.models import HandoffEvent
from xuanpu_server.ids import new_id

if TYPE_CHECKING:
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.handoffs.models import Handoff
    from xuanpu_server.tenancy import Principal


def record_event(tx: CoordinationTransaction, who: Principal, handoff: Handoff, action: str,
                 previous: str, now: float, *, reason: str = '', recipients: tuple[str, ...] = ()) -> None:
    """通用事件只含编号，冻结正文保留在受交接权限保护的领域历史。"""
    identifier = new_id('hev')
    tx.collaboration.events[identifier] = HandoffEvent(tenant_id=tx.tenant_id, id=identifier,
        handoff_id=handoff.id, actor_id=who.user_id, action=action, previous_status=previous,
        next_status=handoff.status, assignee_id=handoff.assignee_id, reason=reason,
        input_digest=handoff.input_digest, material_revision_ids=handoff.material_revision_ids,
        summary=handoff.summary, content_snapshot=handoff.content_snapshot, created_at=now, updated_at=now)
    event = tx.emit('handoff.' + action, handoff.id, handoff.version, actor=who.actor, at=now,
        data={'handoff_id': handoff.id, 'history_id': identifier})
    notify(tx, recipients, event.id, handoff.id, who.user_id, now)


def notify(tx: CoordinationTransaction, recipients: tuple[str, ...], event: str, handoff_id: str,
           actor: str | None, now: float, *, approval_id: str | None = None) -> None:
    """来源和收件人构成稳定键，不依赖最大事件ID，也不向外部发消息。"""
    sequence = max((n.sequence for n in tx.collaboration.notifications.values()), default=0)
    for user in sorted(set(recipients) - {actor}):
        epoch = tx.participant_epochs.get(user)
        if epoch is None:
            raise RuntimeError('协作通知收件人尚未加入当前事务锁集')
        key = hashlib.sha256(f'{tx.tenant_id}:{event}:{user}'.encode()).hexdigest()
        if any(n.semantic_key == key for n in tx.collaboration.notifications.values()):
            continue
        sequence += 1
        tx.collaboration.notifications[key] = CollaborationNotification(tenant_id=tx.tenant_id, id=key,
            user_id=user, user_epoch=epoch, sequence=sequence, handoff_id=handoff_id, approval_id=approval_id,
            source_kind='approval_request' if approval_id else 'handoff', event_id=event, semantic_key=key,
            created_at=now, updated_at=now)
