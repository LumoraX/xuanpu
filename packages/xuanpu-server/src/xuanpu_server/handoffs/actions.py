"""交接原生状态变更；回执、资料授权、责任历史和动态同事务提交。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.approvals.lifecycle import supersede
from xuanpu_server.errors import ConflictError, PermissionDeniedError
from xuanpu_server.handoffs.events import record_event
from xuanpu_server.handoffs.packets import grant_packet, revoke_packets
from xuanpu_server.handoffs.permissions import eligible, owner_source, qualifier
from xuanpu_server.handoffs.schemas import ExplainHandoff, ProvideInformation, TransferHandoff
from xuanpu_server.handoffs.transitions import next_status
from xuanpu_server.shared_matters.source_dependencies import authority_digest

if TYPE_CHECKING:
    from typing import Any

    from pydantic import JsonValue

    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.handoffs.models import Handoff
    from xuanpu_server.handoffs.service import HandoffService
    from xuanpu_server.shared_matters.schemas import SharedChange
    from xuanpu_server.tenancy import Principal


async def mutate(service: HandoffService, who: Principal, identifier: str, action: str,
                  body: SharedChange) -> dict[str, JsonValue]:
    """当前资格失败先持久阻塞，再在事务外返回拒绝，不把失败状态回滚掉。"""
    who = await service.shared.actor(who)
    assert who.user_id is not None
    extra = (body.user_id,) if isinstance(body, TransferHandoff) else ()
    payload: dict[str, JsonValue] = {'action': 'handoff.' + action, 'id': identifier, 'body': body.model_dump(mode='json')}
    blocked = None
    result: dict[str, JsonValue] = {}
    async with service.transaction(who, identifier, extra=extra) as (tx, handoff):
        old = tx.receipt(who.actor, body.request_id, payload)
        if old is not None:
            return old
        if handoff.version != body.expected_version:
            raise ConflictError('交接已改变，请刷新后重新核对')
        target_status = next_status(handoff.status, action)
        _authorize(service, tx, who, handoff, action)
        reason = await service.healthy(tx, handoff)
        if reason is not None and action not in ('cancel', 'transfer', 'provide_information','dispute'):
            service.block(tx, who, handoff, reason)
            blocked = reason
        else:
            previous_grants = {key: grant for key, grant in tx.shared.grants.items() if grant.handoff_id == handoff.id}
            changes = await _changes(service, tx, who, handoff, action, body)
            current_grants = {key: grant for key, grant in tx.shared.grants.items() if grant.handoff_id == handoff.id}
            if previous_grants != current_grants:
                service.shared.bump(tx, tx.shared.matters[handoff.shared_id], now=service.now())
            updated = handoff.model_copy(update={**changes, 'status': target_status,
                'version': handoff.version + 1, 'updated_at': service.now()})
            tx.collaboration.handoffs[identifier] = updated
            recipients = (updated.requester_id,) if action in ('accept','return','start','request_information','confirm_cancel','deliver','reopen') else service.receivers(tx, updated)
            record_event(tx, who, updated, action, handoff.status, service.now(),
                reason=body.reason if isinstance(body, ExplainHandoff) else '', recipients=recipients)
            result = service.receipt(updated)
            tx.record_receipt(who.actor, body.request_id, payload, result)
    if blocked is not None:
        raise ConflictError('交接资格或来源已失效，需要负责人明确重新安排')
    return result


def _authorize(service: HandoffService, tx: CoordinationTransaction, who: Principal, handoff: Handoff, action: str) -> None:
    """发起人、接收人和当前承办人是独立身份，不能代他人接受或签收。"""
    assert who.user_id is not None
    if action in ('cancel', 'provide_information', 'request_cancel', 'transfer', 'sign_off', 'dispute'):
        allowed = who.user_id == handoff.requester_id
    elif action in ('accept', 'return'):
        allowed = eligible(tx, handoff, who.user_id, service.now())
    else:
        allowed = who.user_id == handoff.assignee_id and handoff.assignee_epoch == tx.participant_epochs.get(who.user_id)
    if not allowed:
        raise PermissionDeniedError('当前员工没有这项交接操作权限')
    if handoff.cancel_requested and action in ('deliver', 'start', 'request_information', 'reopen'):
        raise ConflictError('交接正在等待取消确认，不能继续处理或交付')
    if action == 'confirm_cancel' and not handoff.cancel_requested:
        raise ConflictError('发起人尚未提出取消请求')


async def _changes(service: HandoffService, tx: CoordinationTransaction, who: Principal, handoff: Handoff,
             action: str, body: SharedChange) -> dict[str, Any]:
    """资料包用途独立，不授予完整事项editor或全局业务职责。"""
    assert who.user_id is not None
    if action == 'accept':
        grant = grant_packet(tx, handoff, who.user_id, service.now())
        return {'assignee_id': who.user_id, 'assignee_epoch': tx.participant_epochs[who.user_id],
            'assignee_grant_id': grant, 'assignee_authority_digest': authority_digest(tx, who.user_id,
                qualifier(tx, handoff.target_id, service.now()), service.now()) if handoff.target_kind == 'unit' else None}
    if action in ('cancel', 'confirm_cancel', 'return'):
        revoke_packets(tx, handoff.id, service.now())
        supersede(tx,who,handoff.id,service.now())
        return {'cancel_requested': False}
    if action == 'request_cancel':
        return {'cancel_requested': True}
    if action in ('transfer', 'provide_information'):
        assert isinstance(body, ProvideInformation)
        changes = await _new_input(service, tx, who, handoff, body)
        if action == 'transfer':
            assert isinstance(body, TransferHandoff)
            user = await service.shared.identity.get_user(tx.tenant_id, body.user_id)
            if user is None or user.status != 'active' or body.user_id == handoff.requester_id:
                raise ConflictError('请明确另一位启用的接收员工')
            changes.update(target_kind='user', target_id=body.user_id, target_epoch=tx.participant_epochs[body.user_id])
        revoke_packets(tx, handoff.id, service.now())
        changes.update(assignee_id=None, assignee_epoch=None, assignee_grant_id=None,
            assignee_authority_digest=None, cancel_requested=False)
        supersede(tx,who,handoff.id,service.now())
        return {**changes, 'input_digest': service.digest(handoff.model_copy(update=changes))}
    from xuanpu_server.handoffs.delivery import changes as delivery_changes
    return delivery_changes(service,tx,who,handoff,action,body)


async def _new_input(service: HandoffService, tx: CoordinationTransaction, who: Principal,
                     handoff: Handoff, body: ProvideInformation) -> dict[str, Any]:
    """失效原单必须明确重新选择，不能只靠恢复身份或改派悄悄复用旧输入。"""
    assert who.user_id is not None
    source = await service.shared.writable(tx, who, handoff.shared_id, body.shared_version, owner=True)
    source_grant = owner_source(tx, source.id, who.user_id)
    revisions = service.selected_revisions(tx, who, source.id, body.material_revision_ids)
    return {'summary': body.summary, 'content_snapshot': source.content if body.include_content else None,
        'material_revision_ids': revisions, 'source_access_epoch': source.access_epoch,
        'source_grant_id': source_grant.id, 'source_grant_epoch': source_grant.epoch,
        'requester_epoch': tx.participant_epochs[who.user_id], 'blocked_reason': None,
        'policy_version': service.policy_version(tx, handoff.policy_id)}
