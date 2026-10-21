"""会议行动项先验证原文引用，再由本人逐项确认后原子创建原生记录。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, ValidationError

from xuanpu_server.errors import ConflictError, InvalidRequestError
from xuanpu_server.reminders.models import ReminderInput
from xuanpu_server.workspace.creation import create_in_state
from xuanpu_server.workspace.models import validate_data
from xuanpu_server.workspace.rules import audit, get_item, remember

from .models import FrozenModel, Timestamp
from .rules import advance
from .store import require_task, save_record, validate_sources

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.reminders.service import ReminderService
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.workspace.service import WorkspaceService

    from .decisions import ActionChoice, OfficeActionsDecision
    from .models import WorkTaskRecord
    from .store import WorkTaskStore


class SourceQuote(FrozenModel):
    """引用固定输入的字符区间，不把附件中的指令当作授权。"""

    input_name: str = Field(pattern=r'^input_[1-8]\.[a-z0-9]{1,8}$')
    start: int = Field(ge=0, le=200000)
    end: int = Field(ge=1, le=200000)
    quote: str = Field(min_length=1, max_length=2000)


class ActionCandidate(FrozenModel):
    """只提出本人原生待办、日程或提醒，负责人不明时必须保留待核实。"""

    kind: Literal['todo', 'event']
    title: str = Field(min_length=1, max_length=500, pattern=r'\S')
    description: str = Field(default='', max_length=20000)
    owner: Literal['self', 'other', 'unresolved'] = 'unresolved'
    due_at: Timestamp | None = None
    start_at: Timestamp | None = None
    end_at: Timestamp | None = None
    remind_at: Timestamp | None = None
    source: SourceQuote


def anchor_candidate(candidate: ActionCandidate, text: str) -> ActionCandidate:
    """固定原文的唯一完整引用由服务端定位，不采信模型手算偏移；歧义不猜测。"""
    quote = candidate.source
    if quote.end > quote.start and text[quote.start:quote.end] == quote.quote:
        return candidate
    start = text.find(quote.quote)
    if start < 0 or text.find(quote.quote, start + 1) >= 0:
        raise InvalidRequestError('行动项原文不存在或位置不明确，请核实引用')
    return candidate.model_copy(update={'source':quote.model_copy(update={
        'start':start,'end':start + len(quote.quote)})})


def choice_data(candidate: ActionCandidate, choice: ActionChoice, task: WorkTaskRecord,
                now: float) -> dict[str, Any]:
    """编辑只改变允许字段；负责人、来源、时区和事项仍由固定任务约束。"""
    if candidate.owner != 'self':
        raise InvalidRequestError('行动项负责人尚未核定为本人，请先核实')
    data: dict[str, Any] = {'title': choice.title or candidate.title,
        'description': candidate.description if choice.description is None else choice.description,
        'matter_id': task.matter_id}
    keys = ('due_at',) if candidate.kind == 'todo' else ('start_at', 'end_at')
    for key in keys:
        data[key] = getattr(choice, key) if key in choice.model_fields_set else getattr(candidate, key)
        if data[key] is not None and data[key] <= now:
            raise InvalidRequestError('请核实未来的行动时间，不能按已过日期创建')
    if candidate.kind == 'event':
        if data['start_at'] is None or data['end_at'] is None:
            raise InvalidRequestError('日程须先确认明确起止时间')
        data['timezone'] = task.timezone
    return validate_data(candidate.kind, data)


class ActionProposalService:
    """来源、用户决定、原生创建、提醒和任务终态共用同一个员工事务。"""

    def __init__(self, workspace: WorkspaceService, store: WorkTaskStore, *,
                 reminders: ReminderService | None = None, now: Callable[[], float] = time.time) -> None:
        """沿用原生工作区与提醒域，不引入另一套待办或定时系统。"""
        self.workspace, self.store, self.reminders, self.now = workspace, store, reminders, now

    async def prepare(self, who: Principal, task: WorkTaskRecord, actions: list[dict[str, Any]],
                      texts: dict[str, str]) -> tuple[dict[str, Any], ...]:
        """只有精确命中执行前提取原文的有界候选，才能保存为确认卡。"""
        if len(actions) > 32 or len(json.dumps(actions).encode()) > 65536:
            raise InvalidRequestError('会议行动项超限')
        entries = []
        for index, value in enumerate(actions):
            try:
                candidate = ActionCandidate.model_validate(value)
            except ValidationError as exc:
                raise InvalidRequestError('行动项候选字段无效') from exc
            name = candidate.source.input_name
            if name not in {entry.relative_name for entry in task.inputs}:
                raise InvalidRequestError('行动项必须引用固定输入中真实存在的原文')
            candidate = anchor_candidate(candidate, texts.get(name, ''))
            identifier = 'action_' + hashlib.sha256(f'{task.task_id}:{index}'.encode()).hexdigest()[:24]
            entries.append({'action_id': identifier, 'proposal_id': f'{task.task_id}:{index}',
                            'candidate': candidate.model_dump(mode='json')})
        if entries:
            current = await self.store.get(who, task.task_id)
            await self.store.transition(who, task.task_id, 'running', expected_version=current.state_version,
                at=self.now(), event_kind='actions_prepared', action_candidates=tuple(entries),
                proposal_ids=tuple(entry['proposal_id'] for entry in entries), decisions_complete=False)
        return tuple(entries)

    async def confirm(self, who: Principal, task_id: str, decision: OfficeActionsDecision) -> WorkTaskRecord:
        """完整重试回放先于版本检查，全部选择在同一事务提交或整体回滚。"""
        task = await self.store.get(who, task_id)
        fingerprint = hashlib.sha256(decision.model_dump_json(exclude_unset=True).encode()).hexdigest()
        prior = task.decision_fingerprints.get(decision.request_id)
        if prior is not None:
            if prior != fingerprint:
                raise ConflictError('同一决定请求的内容不同')
            return task
        prepared = await self._reminders(who, task, decision)
        async with self.workspace.transaction(who, 'work-task:office-decision') as state:
            current = require_task(state, task_id)
            prior = current.decision_fingerprints.get(decision.request_id)
            if prior is not None:
                if prior != fingerprint:
                    raise ConflictError('同一决定请求的内容不同')
                return current
            if (current.status != 'waiting_user' or current.state_version != decision.state_version
                or not current.outputs_verified or current.cancel_requested):
                raise ConflictError('任务当前不等待该行动项决定')
            validate_sources(state, current)
            for source in current.inputs:
                if get_item(state, source.item_id)['data']['current_revision'] != source.revision:
                    raise ConflictError('来源材料已更新，请重新生成候选')
            choices = {choice.action_id: choice for choice in decision.choices}
            known = {entry['action_id'] for entry in current.action_candidates}
            if len(choices) != len(decision.choices) or set(choices) - known:
                raise InvalidRequestError('行动项选择重复或不属于当前任务')
            results = self._apply(state, who, current, choices, prepared)
            updated = advance(current, 'succeeded', expected_version=current.state_version, at=self.now(),
                decisions_complete=True, action_results=tuple(results), pending=(),
                decision_fingerprints={**current.decision_fingerprints, decision.request_id: fingerprint})
            save_record(state, updated, 'actions_confirmed')
            return updated

    async def _reminders(self, who: Principal, task: WorkTaskRecord, decision: OfficeActionsDecision) -> dict[str, Any]:
        """事务外准备明确请求的提醒，真正创建和配额检查仍在员工事务内。"""
        prepared: dict[str, Any] = {}
        choices = {choice.action_id: choice for choice in decision.choices if choice.accepted}
        for entry in task.action_candidates:
            candidate = ActionCandidate.model_validate(entry['candidate'])
            if entry['action_id'] not in choices:
                continue
            choice = choices[entry['action_id']]
            remind_at = choice.remind_at if 'remind_at' in choice.model_fields_set else candidate.remind_at
            if remind_at is None:
                continue
            if self.reminders is None:
                raise InvalidRequestError('原生提醒服务不可用')
            choice = choices[entry['action_id']]
            prepared[entry['action_id']] = await self.reminders.prepare(who,
                ReminderInput(title=choice.title or candidate.title, due_at=remind_at,
                              timezone=task.timezone), 'task-reminder:' + entry['proposal_id'])
        return prepared

    def _apply(self, state: Any, who: Principal, task: WorkTaskRecord,
               choices: dict[str, ActionChoice], prepared: dict[str, Any]) -> list[dict[str, Any]]:
        """原生创建复用事务内主体，禁止在员工锁内再次调用公开service取锁。"""
        results = []
        for entry in task.action_candidates:
            choice = choices.get(entry['action_id'])
            if choice is None or not choice.accepted:
                result = {'action_id': entry['action_id'], 'accepted': False}
                remember(state, 'proposal:' + entry['proposal_id'], {'action':'cancel', 'data':{}},
                         {'state':'cancelled'})
            else:
                candidate = ActionCandidate.model_validate(entry['candidate'])
                data = choice_data(candidate, choice, task, self.now())
                record = create_in_state(state, who.actor, candidate.kind, data,
                    entry['proposal_id'] + ':create', self.now(), entry['proposal_id'])
                if task.trigger_origin_chain:
                    state.items[record['id']]['_trigger_origin_chain']=list(task.trigger_origin_chain)
                result = {'action_id':entry['action_id'], 'accepted':True, 'item_id':record['id'],
                          'kind':record['kind'], 'title':record['data']['title']}
                reminder = prepared.get(entry['action_id'])
                if reminder is not None:
                    state.pending_reminders.append(asdict(reminder))
                    state.pending_reminder_actors[reminder.reminder_id] = who.actor
                    result['reminder_id'] = reminder.reminder_id
            results.append(result)
        audit(state, who.actor, 'work_task_actions_confirmed', self.now(), {'id':task.task_id, 'kind':'work_task'})
        return results
