"""用户决定协调器：工作区、跟进、提醒及回执在员工事务内提交。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import ConflictError, InvalidRequestError, NotFoundError
from xuanpu_server.proactive.rules import TERMINAL, stable_id
from xuanpu_server.reminders.models import ReminderInput
from xuanpu_server.workspace.rules import apply_todo_change, audit, remember, replay

if TYPE_CHECKING:
    from xuanpu_server.proactive.models import ActionInput
    from xuanpu_server.proactive.service import ProactiveService
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.workspace.models import WorkspaceState


async def apply_action(service: ProactiveService, who: Principal, record_id: str, body: ActionInput) -> dict[str, Any]:
    """真实点击和对话确认共用幂等键，不允许模型直接改变业务状态。"""
    tenant, user = await service.workspace.require_employee(who)
    key = 'proactive:' + body.request_id
    payload = {'id': record_id, **body.model_dump()}
    prepared = None
    if body.action == 'set_reminder':
        if body.due_at is None or service.reminders is None:
            raise InvalidRequestError('请确认未来的提醒时间')
        before_record = await service.get(who, record_id)
        prepared = await service.reminders.prepare(who, ReminderInput(title='跟进：' + before_record['title'],
            due_at=body.due_at, timezone=body.timezone, channels=body.channels), key)
    now = service.now()
    async with service.transaction(tenant, user, key) as state:
        prior = replay(state, key, payload)
        if prior is not None:
            return prior
        record = state.checks.get(record_id) or state.followups.get(record_id)
        if record is None:
            raise NotFoundError('业务跟进不存在')
        if record['version'] != body.version:
            raise ConflictError('跟进状态已变化，请刷新后重试')
        if record['status'] in TERMINAL:
            raise ConflictError('该业务跟进已处理，请查看最新状态')
        old_version = record['version']
        _apply(service, state, record, body, now)
        if prepared is not None:
            if record.get('reminder_id'):
                state.pending_reminder_cancellations.append(record['reminder_id'])
            state.pending_reminders.append(asdict(prepared))
            record['reminder_id'] = prepared.reminder_id
        record.update(version=old_version + 1, updated_at=now, handled_at=now)
        audit(state, who.actor, 'proactive_' + body.action, now, record)
        labels = {'complete': '已确认完成', 'postpone': '已延后', 'ignore_stage': '本阶段不再跟进',
            'set_reminder': '已设置提醒', 'progress': '已记录新进展'}
        state.messages.append({'id': state.next_message_id, 'request_id': key + ':receipt',
            'role': 'assistant', 'text': labels[body.action] + '：' + record['title'] + '。',
            'attachments': [], 'matter_id': None, 'created_at': now,
            'metadata': {'proactive_id': record['id'], 'action': body.action, 'status': record['status']}})
        state.next_message_id += 1
        result = service.view(record, state)
        remember(state, key, payload, result)
        return result


def _apply(service: ProactiveService, state: WorkspaceState, record: dict[str, Any], body: ActionInput, now: float) -> None:
    """仅修改用户明确选择的字段，保留已核实和推测的边界。"""
    item = state.items.get(record.get('item_id', ''))
    if item and body.item_version is not None and item['version'] != body.item_version:
        raise ConflictError('待办已更新，请重新确认')
    if body.action in ('postpone', 'set_reminder'):
        if body.due_at is None or not now < body.due_at <= now + 5 * 366 * 86400:
            raise InvalidRequestError('请设置未来五年内的跟进时间')
        if item and body.action == 'postpone':
            apply_todo_change(state, item, {'due_at': body.due_at}, now)
        # 规则记录保留原阶段；用户安排后的后续由关联语义记录承接。
        if record['source'] == 'proactive_check':
            _promote(state, record, now)
        if record.get('reminder_id'):
            state.pending_reminder_cancellations.append(record['reminder_id'])
            record.pop('reminder_id', None)
        record.update(status='waiting', next_review_at=body.due_at, due_at=body.due_at,
            body='已安排后续跟进，到时会再次核实进展。')
    elif body.action == 'complete':
        if item:
            apply_todo_change(state, item, {'status': 'done'}, now)
        record['status'] = 'resolved'
        record['handled_at'] = now
    elif body.action == 'progress':
        if record.get('reminder_id'):
            state.pending_reminder_cancellations.append(record.pop('reminder_id'))
        if item:
            apply_todo_change(state, item, {}, now, progress=True)
        record.update(status='waiting', next_review_at=now + 86400, body='已记录进展，稍后继续跟进。')
        if record['source'] == 'proactive_check':
            _promote(state, record, now)
    else:
        record.update(status='ignored' if record['source'] == 'proactive_check' else 'dismissed', handled_at=now)
    if item and record['source'] == 'proactive_followup':
        record.update(generation=item.get('generation', 1), item_version=item['version'])
    if body.action in ('complete', 'ignore_stage') and record.get('reminder_id'):
        state.pending_reminder_cancellations.append(record['reminder_id'])


def _promote(state: WorkspaceState, record: dict[str, Any], now: float) -> None:
    """规则阶段保持历史，用户后续安排成为独立可等待的跟进。"""
    original_id = record['id']
    original = deepcopy(record)
    original.update(status='resolved', updated_at=now, version=original['version'] + 1)
    state.checks[original_id] = original
    record.update(id=stable_id('pf_', original_id, 'arranged'), source='proactive_followup', kind='commitment')
    state.followups[record['id']] = record
