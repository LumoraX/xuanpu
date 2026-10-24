"""审批和交付核对相同的固定ready成果，不把任务成功当签收。"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, InvalidRequestError

if TYPE_CHECKING:
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.handoffs.models import Handoff
    from xuanpu_server.handoffs.schemas import ResultRef
    from xuanpu_server.handoffs.service import HandoffService
    from xuanpu_server.tenancy import Principal


def results(service: HandoffService, tx: CoordinationTransaction, who: Principal, handoff: Handoff,
            refs: tuple[ResultRef, ...]) -> tuple[str, ...]:
    """只接受可读的当前ready版本与实际SHA，拒绝同材料重复或跨事项混入。"""
    if len({ref.material_id for ref in refs}) != len(refs) or not refs:
        raise InvalidRequestError('请明确一至八项不重复的固定成果')
    identifiers = []
    for ref in refs:
        _, revision = service.materials.readable(tx, who, handoff.shared_id, ref.material_id, ref.revision)
        if revision.sha256 != ref.sha256:
            raise ConflictError('成果摘要与固定版本不一致')
        identifiers.append(revision.id)
    return tuple(identifiers)


def target_digest(handoff: Handoff, refs: tuple[ResultRef, ...]) -> str:
    """目标输入、责任、版本及固定成果都参与组织批准，不授权日后新目标。"""
    body = {'handoff_id':handoff.id,'version':handoff.version,'input_digest':handoff.input_digest,
        'assignee_id':handoff.assignee_id,'assignee_epoch':handoff.assignee_epoch,
        'results':sorted((ref.model_dump(mode='json') for ref in refs),key=lambda r:str(r['material_id']))}
    return hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()
