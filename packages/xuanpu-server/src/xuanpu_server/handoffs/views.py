"""来件只显示明确摘要，正文与可用动作依当前来源和真人责任投影。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.handoffs.permissions import assignee_current, eligible

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.handoffs.models import Handoff
    from xuanpu_server.handoffs.service import HandoffService
    from xuanpu_server.tenancy import Principal


def actions(service: HandoffService, tx: CoordinationTransaction, who: Principal, handoff: Handoff) -> list[str]:
    """角色只由当前服务端事实决定，关闭新建不妨碍既有事项解困。"""
    assert who.user_id is not None
    owner = who.user_id == handoff.requester_id
    assignee = assignee_current(tx, handoff, who.user_id, service.now())
    result = _owner_pending_actions(handoff) if owner else []
    if handoff.blocked_reason is not None:
        if owner and handoff.status=='delivered':
            result.append('dispute')
        return result
    if handoff.status == 'submitted' and eligible(tx, handoff, who.user_id, service.now()):
        result.extend(('accept', 'return'))
    if owner and handoff.status in ('accepted', 'in_progress', 'needs_information') and not handoff.cancel_requested:
        result.append('request_cancel')
    if assignee:
        result.extend(_assignee_actions(handoff))
    if owner and handoff.status == 'delivered':
        result.extend(('sign_off', 'dispute'))
    return result


def _owner_pending_actions(handoff: Handoff) -> list[str]:
    """发起人解困动作保留明确新输入，既有交付历史不能删除。"""
    result: list[str] = []
    if handoff.status == 'submitted':
        result.append('cancel')
    if handoff.status in ('accepted', 'in_progress', 'needs_information','disputed'):
        result.append('transfer')
    if handoff.status == 'needs_information':
        result.append('provide_information')
    return result


def _assignee_actions(handoff: Handoff) -> list[str]:
    """承办人当前状态动作不混入发起人的签收或组织决定。"""
    if handoff.cancel_requested:
        return ['confirm_cancel']
    result={'accepted':['start','request_information'],
        'in_progress':['request_information','publish_result','deliver'],
        'disputed':['reopen']}.get(handoff.status,[]).copy()
    if handoff.status=='in_progress' and handoff.policy_id is not None:
        result.append('submit_approval')
    return result


def view(service: HandoffService, tx: CoordinationTransaction, who: Principal, handoff: Handoff) -> dict[str, JsonValue]:
    """受理前不输出原正文，撤权后只返回解困所需元数据。"""
    result = handoff.model_dump(mode='json', exclude={'source_grant_id', 'source_grant_epoch',
        'assignee_authority_digest', 'assignee_grant_id'})
    readable = handoff.blocked_reason is None and (who.user_id == handoff.requester_id or
        assignee_current(tx, handoff, who.user_id or '', service.now()))
    if not readable:
        result['content_snapshot'] = None
    if handoff.blocked_reason is not None:
        result.update(title='已结束工作的历史记录' if handoff.status in ('signed_off','returned','cancelled') else '交接待重新安排', summary='', material_revision_ids=[])
    history: list[JsonValue] = []
    for event in sorted(tx.collaboration.events.values(), key=lambda e: (e.created_at, e.id)):
        if event.handoff_id != handoff.id:
            continue
        record = event.model_dump(mode='json')
        if not readable:
            record.update(summary='', content_snapshot=None, material_revision_ids=[], reason='')
        history.append(record)
    result['deliveries'] = [d.model_dump(mode='json',exclude={'result_refs'}) | {
        'result_refs':[r.model_dump(mode='json') for r in d.result_refs] if readable else []}
        for d in sorted(tx.collaboration.deliveries.values(),key=lambda d:d.delivery_version) if d.handoff_id==handoff.id]
    result['history'] = history
    result['allowed_actions'] = list(actions(service, tx, who, handoff))
    return result
