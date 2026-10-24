"""解析管理员冻结的步骤，不允许申请人或模型补选审批人。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.approvals.models import ApprovalStep, FrozenSelector
from xuanpu_server.errors import ConflictError, InvalidRequestError
from xuanpu_server.handoffs.permissions import qualifier
from xuanpu_server.shared_matters.permissions import grant_active
from xuanpu_server.shared_matters.source_dependencies import authority_digest

if TYPE_CHECKING:
    from xuanpu_server.approvals.schemas import StepSelector
    from xuanpu_server.coordination.models import CoordinationTransaction


def candidates(tx: CoordinationTransaction, selectors: tuple[StepSelector, ...]) -> set[str]:
    """候选只用于锁定；锁内仍要求当前身份和唯一岗位匹配。"""
    found = {s.user_id for s in selectors if s.user_id is not None}
    units = {s.unit_id for s in selectors if s.selector == 'unit_role'}
    if units:
        found.update(a.user_id for a in tx.organization.roles.values() if a.status == 'active' and a.role == 'unit_approver')
    return found


def freeze(tx: CoordinationTransaction, selectors: tuple[StepSelector, ...]) -> tuple[FrozenSelector, ...]:
    """指定人绑定当前身份代次，单位规则只能引用启用的明确单位。"""
    result = []
    for selector in selectors:
        epoch = None
        if selector.selector == 'user':
            user = selector.user_id or ''
            if tx.participant_statuses.get(user) != 'active':
                raise ConflictError('审批人必须是启用员工')
            epoch = tx.participant_epochs[user]
        else:
            unit = tx.organization.units.get(selector.unit_id or '')
            if unit is None or unit.status != 'active':
                raise InvalidRequestError('审批部门不可用')
        result.append(FrozenSelector(**selector.model_dump(), user_epoch=epoch))
    return tuple(result)


def resolve(tx: CoordinationTransaction, selectors: tuple[FrozenSelector, ...], applicant: str, now: float) -> tuple[ApprovalStep, ...]:
    """每步唯一启用员工且不得自批，歧义要求管理员修改后重提。"""
    if not candidates(tx, selectors) <= tx.participant_epochs.keys():
        raise ConflictError('审批候选已变化，请重新核对规则后重提')
    steps = []
    for index, selector in enumerate(selectors):
        if selector.selector == 'user':
            user = selector.user_id or ''
            if tx.participant_statuses.get(user) != 'active' or tx.participant_epochs.get(user) != selector.user_epoch:
                raise ConflictError('策略指定审批人身份已失效，请管理员重新确认规则')
            digest = ''
        else:
            grant = qualifier(tx, selector.unit_id or '', now, 'unit_approver')
            users = [u for u in sorted(tx.participant_epochs) if grant_active(tx, grant, u, now)]
            if len(users) != 1:
                raise ConflictError('审批职责未匹配唯一员工，请管理员明确规则后重提')
            user = users[0]
            digest = authority_digest(tx, user, grant, now)
        if user == applicant:
            raise ConflictError('申请人不能审批自己的成果')
        steps.append(ApprovalStep(index=index, user_id=user, user_epoch=tx.participant_epochs[user],
            selector=selector, authority_digest=digest))
    return tuple(steps)


def step_current(tx: CoordinationTransaction, step: ApprovalStep, now: float) -> bool:
    """同名员工或重新授予同一职责均不复活已失效的旧步骤。"""
    if tx.participant_statuses.get(step.user_id) != 'active' or tx.participant_epochs.get(step.user_id) != step.user_epoch:
        return False
    if step.selector.selector == 'user':
        return True
    grant = qualifier(tx, step.selector.unit_id or '', now, 'unit_approver')
    return grant_active(tx, grant, step.user_id, now) and authority_digest(tx, step.user_id, grant, now) == step.authority_digest
