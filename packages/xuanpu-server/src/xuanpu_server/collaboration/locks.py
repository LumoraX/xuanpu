"""用途读取只锁实际授权与材料来源需要的身份，不遍历员工社交关系。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError

if TYPE_CHECKING:
    from xuanpu_server.coordination.models import CoordinationState
    from xuanpu_server.shared_matters.models import SharedAccessGrant


def purpose_participants(state: CoordinationState, participants: tuple[str, ...]) -> tuple[str, ...]:
    """起点是调用员工的有效用途，递归仅沿固定材料来源，不沿新增人员其他工作。"""
    found = set(participants)
    pending = [g for g in state.shared.grants.values() if g.status == 'active'
        and g.subject_kind == 'user' and g.subject_id in found and g.handoff_id is not None]
    visited: set[str] = set()
    while pending:
        grant = pending.pop()
        if grant.id in visited:
            continue
        visited.add(grant.id)
        if len(visited) > 2000 or len(found) > 2000:
            raise ConflictError('协作用途来源集合超过当前试点上限')
        pending.extend(_dependencies(state, grant, found))
    return tuple(sorted(found))


def _dependencies(state: CoordinationState, grant: SharedAccessGrant, found: set[str]) -> list[SharedAccessGrant]:
    """来源负责人独立事项授权与本次固定资料的依赖分别计算。"""
    handoff = state.collaboration.handoffs.get(grant.handoff_id or '')
    if handoff is None or handoff.status in ('cancelled', 'returned') or handoff.blocked_reason is not None:
        return []
    found.add(handoff.requester_id)
    if handoff.assignee_id is not None:
        found.add(handoff.assignee_id)
    revisions = list(handoff.material_revision_ids)
    readers = {handoff.requester_id}
    if grant.approval_id is not None:
        request = state.collaboration.approvals.get(grant.approval_id)
        if request is not None:
            found.add(request.applicant_id)
            found.update(s.user_id for s in request.steps)
            readers.add(request.steps[request.current_step].user_id)
            revisions.extend(r.id for r in state.shared.revisions.values() if any(
                r.material_id==ref.material_id and r.revision==ref.revision for ref in request.result_refs))
    sources: set[str] = set()
    checked: set[str] = set()
    while revisions:
        identifier = revisions.pop()
        if identifier in checked:
            continue
        checked.add(identifier)
        revision = state.shared.revisions.get(identifier)
        if revision is not None:
            sources.update(ref.shared_id for ref in revision.source_refs)
            revisions.extend(ref.material_revision_id for ref in revision.source_refs if ref.material_revision_id is not None)
    return [g for g in state.shared.grants.values() if g.status == 'active' and g.handoff_id is not None
        and g.subject_kind == 'user' and g.subject_id in readers and g.matter_id in sources]
