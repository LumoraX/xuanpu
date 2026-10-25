"""提交边界的共用不变量，不依赖调用者自觉保留不可变历史。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.capabilities.store import validate_state as validate_capabilities
from xuanpu_server.collaboration.store import validate_history as validate_collaboration_history
from xuanpu_server.errors import ConflictError
from xuanpu_server.organization.store import validate_organization
from xuanpu_server.shared_matters.store import validate_history

if TYPE_CHECKING:
    from xuanpu_server.coordination.models import CoordinationState

def validate_commit(tenant: str, before: CoordinationState, after: CoordinationState) -> None:
    """领域记录不能物理删除；已提交回执、事件和确认都只能追加。"""
    validate_organization(tenant, after.organization)
    validate_capabilities(tenant, before.capabilities, after.capabilities)
    validate_history(before.shared, after.shared)
    validate_collaboration_history(before.collaboration, after.collaboration)
    for field in type(before.organization).model_fields:
        previous = getattr(before.organization, field)
        current = getattr(after.organization, field)
        if previous.keys() - current.keys():
            raise ConflictError('组织记录只能撤销或停用，不能删除')
    for previous, current in ((before.receipts, after.receipts), (before.events, after.events)):
        if any(key not in current or current[key] != record for key, record in previous.items()):
            raise ConflictError('已提交回执和事件不可改写')
    if any(not ids <= after.acknowledgements.get(consumer, set())
           for consumer, ids in before.acknowledgements.items()):
        raise ConflictError('已确认事件不可取消确认')
