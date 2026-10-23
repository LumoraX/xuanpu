"""身份事务生成稳定治理事件，身份提交后再由协调消费者收尾。"""
from __future__ import annotations

import hashlib
import json

from xuanpu_server.coordination.outbox import CoordinationEvent


def disabled_event(tenant: str, user_id: str, user_epoch: int, at: float) -> CoordinationEvent:
    """用身份权限版本去重，重复停用不能伪造新的生命周期。"""
    semantic = json.dumps([tenant, 'user_disabled', user_id, user_epoch], ensure_ascii=False)
    return CoordinationEvent(id='evt_' + hashlib.sha256(semantic.encode()).hexdigest(),
        tenant_id=tenant, kind='user_disabled', entity_id=user_id, entity_version=user_epoch,
        actor='system:identity', at=at, data={'user_id': user_id, 'user_epoch': user_epoch})
