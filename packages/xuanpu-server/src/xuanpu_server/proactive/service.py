"""持久主动业务服务：员工事务内认领、核验、去重及通知。"""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import NotFoundError, PermissionDeniedError
from xuanpu_server.proactive.inputs import commit_cursors, context_current, prepare_context
from xuanpu_server.proactive.models import absolute_time
from xuanpu_server.proactive.rules import TERMINAL, alert_time, enabled, stable_id, todo_check
from xuanpu_server.proactive.semantic import validate_assessment
from xuanpu_server.workspace.models import fingerprint
from xuanpu_server.workspace.rules import audit

if TYPE_CHECKING:
    import builtins
    from collections.abc import AsyncIterator, Awaitable, Callable

    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.identity.store import IdentityStore
    from xuanpu_server.proactive.models import ActionInput, Assessment
    from xuanpu_server.reminders.service import ReminderService
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.workspace.models import WorkspaceState
    from xuanpu_server.workspace.service import WorkspaceService

logger = logging.getLogger(__name__)


class ProactiveService:
    """后台使用明确租户/员工上下文，不复用 HTTP 授权或持有长事务。"""

    def __init__(self, *, workspace: WorkspaceService, control: ControlPlaneStore,
                 identity: IdentityStore, sample: Callable[[str, str, str], Awaitable[str]],
                 now: Callable[[], float], semantic_interval: float = 1800, reminders: ReminderService | None = None) -> None:
        """依赖与时钟注入；普通员工无需提供配置。"""
        self.workspace, self.control, self.identity = workspace, control, identity
        self.sample, self.now, self.semantic_interval = sample, now, semantic_interval
        self.reminders = reminders
        self._owner_cursors: dict[str, tuple[str, str] | None] = {}

    async def require_owner(self, tenant: str, user: str) -> None:
        """每次后台读写都核实租户和员工当前状态。"""
        organization = await self.control.get_tenant(tenant)
        employee = await self.identity.get_user(tenant, user)
        if not organization or organization.status != 'active' or not employee or employee.status != 'active':
            raise PermissionDeniedError('员工或租户不可用')

    @asynccontextmanager
    async def transaction(self, tenant: str, user: str, request_id: str | None = None) -> AsyncIterator[WorkspaceState]:
        """同一员工的规则、跟进和用户动作共用数据库锁顺序。"""
        async def guard() -> None:
            """在内存提交时也重新核实活动身份。"""
            await self.require_owner(tenant, user)
        async with self.workspace.store.transaction(tenant, user, guard, request_id) as state:
            yield state

    async def list(self, who: Principal) -> builtins.list[dict[str, Any]]:
        """只返回本人最近一百个关注事项，不返回内部租约或模型原文。"""
        tenant, user = await self.workspace.require_employee(who)
        async with self.transaction(tenant, user) as state:
            records = [*state.checks.values(), *state.followups.values()]
            return [self.view(record, state) for record in sorted(records,
                key=lambda row: (row.get('updated_at', 0), row['id']), reverse=True)[:100]]

    async def get(self, who: Principal, record_id: str) -> dict[str, Any]:
        """未知或他人的标识统一不可见。"""
        tenant, user = await self.workspace.require_employee(who)
        async with self.transaction(tenant, user) as state:
            record = state.checks.get(record_id) or state.followups.get(record_id)
            if record is None:
                raise NotFoundError('业务跟进不存在')
            return self.view(record, state)

    @staticmethod
    def view(record: dict[str, Any], state: WorkspaceState) -> dict[str, Any]:
        """输出副本，并投影最新工作区版本供用户确认。"""
        result = deepcopy(record)
        item = state.items.get(record.get('item_id', ''))
        if item:
            result['item_version'] = item['version']
        return {key: value for key, value in result.items() if not key.startswith('_')}

    async def act(self, who: Principal, record_id: str, body: ActionInput) -> dict[str, Any]:
        """用户已确认的动作进入共用事务协调器。"""
        from xuanpu_server.proactive.actions import apply_action
        return await apply_action(self, who, record_id, body)

    async def run_owners(self, kind: str) -> None:
        """每轮只遍历一页员工，分别保存规则和语义的续扫游标。"""
        owners = await self.workspace.store.owners(self._owner_cursors.get(kind), 100)
        for tenant, user in owners:
            try:
                if kind == 'rules':
                    await self.scan_owner(tenant, user)
                else:
                    await self.evaluate_owner(tenant, user)
            except Exception:
                logger.exception('proactive_%s_owner_failed', kind)
        self._owner_cursors[kind] = owners[-1] if len(owners) == 100 else None

    def notify(self, state: WorkspaceState, record: dict[str, Any], key: str, now: float) -> None:
        """同事务保存核心通知和可选渠道，不经模型决定投递权限。"""
        source = record['source']
        state.pending_notifications.append({
            'notification_key': key, 'source': source,
            'proactive_check_id': record['id'] if source == 'proactive_check' else None,
            'proactive_followup_id': record['id'] if source == 'proactive_followup' else None,
            'title': record['title'], 'body': record['body'][:4000], 'due_at': record.get('due_at'),
            'created_at': now, 'alert_after': alert_time(state.settings, now),
            'channels': state.settings.get('proactive_channels', ['inbox']),
        })
        record['last_notified_at'] = now
        audit(state, 'system:proactive', 'proactive_notified', now, record)

    async def scan_owner(self, tenant: str, user: str) -> None:
        """规则扫描与已安排的跟进均不依赖模型可用。"""
        await self.require_owner(tenant, user)
        now = self.now()
        async with self.transaction(tenant, user) as state:
            if not enabled(state.settings, 'rules', now):
                return
            cursor = state.scan_state.get('rule_cursor', '')
            items = sorted((item for item in state.items.values() if item['id'] > cursor), key=lambda item: item['id'])[:100]
            for item in items:
                candidate = todo_check(item, now)
                if candidate and enabled(state.settings, candidate['kind'], now):
                    self._check(state, tenant, user, candidate, now)
            state.scan_state['rule_cursor'] = items[-1]['id'] if len(items) == 100 else ''
            self._due_followups(state, now)
            self._resolve_closed(state, now)

    def _check(self, state: WorkspaceState, tenant: str, user: str, candidate: dict[str, Any], now: float) -> None:
        """同一代际和阶段只检查一次，模型与规则命中同一待办时合并。"""
        record_id = stable_id('pc_', tenant, user, candidate['item_id'], candidate['generation'], candidate['kind'], candidate['stage_index'])
        if record_id in state.checks:
            return
        linked = next((row for row in state.followups.values() if row.get('item_id') == candidate['item_id'] and row['status'] not in TERMINAL), None)
        record = {**candidate, 'id': record_id, 'version': 1, 'status': 'notified',
            'source': 'proactive_check', 'created_at': now, 'updated_at': now}
        state.checks[record_id] = record
        if linked:
            record['status'] = 'resolved'
            # 已有业务跟进承接用户安排；到新时间由它或关联原生提醒单独核实。
            if linked['status'] == 'waiting' or now - linked.get('last_notified_at', 0) < 86400:
                return
            linked.update(reason=record['reason'], body=record['body'], updated_at=now, version=linked['version'] + 1)
            self.notify(state, linked, 'rule:' + record_id, now)
            return
        self.notify(state, record, record_id, now)

    def _due_followups(self, state: WorkspaceState, now: float) -> None:
        """用户已安排的时间到达后核实进展，单个阶段通知一次。"""
        for record in state.followups.values():
            if record['status'] != 'waiting' or record.get('next_review_at', now + 1) > now:
                continue
            if not enabled(state.settings, record['kind'], now):
                continue
            item = state.items.get(record.get('item_id', ''))
            if item and (item.get('trashed_at') is not None or item['data'].get('status') == 'done'):
                record.update(status='resolved', updated_at=now, version=record['version'] + 1)
                continue
            record.update(status='awaiting_user', body='安排的跟进时间到了，这件事完成了吗？需要继续处理或调整时间吗？',
                updated_at=now, version=record['version'] + 1)
            if not record.get('reminder_id'):
                self.notify(state, record, f"{record['id']}:scheduled:{record['next_review_at']}", now)

    @staticmethod
    def _resolve_closed(state: WorkspaceState, now: float) -> None:
        """未通过主动入口完成或回收的待办也会停止旧跟进。"""
        for record in [*state.checks.values(), *state.followups.values()]:
            item = state.items.get(record.get('item_id', ''))
            if item and (item.get('trashed_at') is not None or item['data'].get('status') == 'done') and record['status'] not in TERMINAL:
                record.update(status='resolved', version=record['version'] + 1, updated_at=now)

    @staticmethod
    def _snapshot(state: WorkspaceState) -> str:
        """任务版本、最新消息与用户设置共同约束模型结果提交。"""
        return fingerprint({'items': [(item['id'], item['version']) for item in state.items.values()],
            'messages': state.next_message_id, 'settings': state.settings,
            'followups': [(row['id'], row['version']) for row in state.followups.values()]})

    async def _claim(self, tenant: str, user: str) -> dict[str, Any] | None:
        """持久语义评估租约，不在数据库事务中等待模型。"""
        now = self.now()
        async with self.transaction(tenant, user) as state:
            scan = state.scan_state
            if not enabled(state.settings, 'semantic', now) or scan.get('lease_until', 0) > now or scan.get('next_sample_at', 0) > now:
                return None
            snapshot = self._snapshot(state)
            token = secrets.token_hex(16)
            scan.update(claim_token=token, lease_until=now + 90, next_sample_at=now + self.semantic_interval)
            return {'token': token, 'snapshot': snapshot, 'items': deepcopy(list(state.items.values())),
                'followups': deepcopy(list(state.followups.values())), 'settings': deepcopy(state.settings),
                'message_cursor': scan.get('message_cursor', 0), 'next_message_id': state.next_message_id,
                'business_cursors': deepcopy(scan.get('business_cursors', {})),
                'item_cursor': scan.get('item_cursor', ''),
                'partial_offsets': deepcopy(scan.get('partial_offsets', {})),
                'session_cursor': scan.get('session_cursor')}

    async def evaluate_owner(self, tenant: str, user: str) -> bool:
        """语义评估失败保留明确错误和可恢复游标，绝不冒充已经检查。"""
        claim: dict[str, Any] | None = None
        try:
            await self.require_owner(tenant, user)
            claim = await self._claim(tenant, user)
            if claim is None:
                return False
            payload, evidence = await prepare_context(self, tenant, user, claim)
            if not evidence:
                assessment = validate_assessment('{"followups":[]}', evidence)
            else:
                text = await self.sample(tenant, user, payload)
                assessment = validate_assessment(text, evidence)
            if not await context_current(self, tenant, user, claim):
                raise ValueError('业务会话在判断期间已更新')
            return await self._commit(tenant, user, claim, assessment, evidence)
        except PermissionDeniedError:
            return False
        except Exception as exc:
            logger.warning('proactive_semantic_failed: %s', type(exc).__name__)
            if claim:
                await self._failed(tenant, user, claim, type(exc).__name__)
            return False

    async def _commit(self, tenant: str, user: str, claim: dict[str, Any], assessment: Assessment,
                      evidence: dict[str, dict[str, Any]]) -> bool:
        """结果提交前复核身份、快照和租约；过期证据不形成业务判断。"""
        now = self.now()
        async with self.transaction(tenant, user) as state:
            scan = state.scan_state
            if scan.get('claim_token') != claim['token'] or scan.get('lease_until', 0) <= now:
                return False
            if self._snapshot(state) != claim['snapshot']:
                scan.update(lease_until=0, next_sample_at=now + 60, last_error='stale_snapshot')
                return False
            for candidate in assessment.followups:
                self._followup(state, tenant, user, candidate.model_dump(), evidence, now)
            for record in state.followups.values():
                if record['status'] == 'awaiting_user' and (record.get('next_review_at') or 0) <= now and any(ref in evidence for ref in record['evidence_refs']):
                    # 即使模型本轮无需再提醒，也推进已读事项复查时间，让后续事项得到公平评估。
                    record.update(next_review_at=now + 86400, version=record['version'] + 1)
                if record['status'] not in TERMINAL:
                    session_ids = {ref.split(':')[1] for ref in record['evidence_refs'] if ref.startswith('business:')}
                    recent = [ref for ref, entry in evidence.items() if entry.get('kind') == 'message'
                        and (ref.startswith('message:') or entry.get('session_id') in session_ids)]
                    record['context_refs'] = list(dict.fromkeys([*record.get('context_refs', []), *recent]))[-10:]
            scan.update(lease_until=0, claim_token=None, last_error=None, attempts=0,
                last_sample_at=now, last_snapshot=self._snapshot(state), session_cursor=claim['next_session_cursor'])
            commit_cursors(scan, claim, evidence)
            return True

    def _followup(self, state: WorkspaceState, tenant: str, user: str, candidate: dict[str, Any],
                  evidence: dict[str, dict[str, Any]], now: float) -> None:
        """稳定事项身份、证据版本和用户决定共同避免重复催办。"""
        if not enabled(state.settings, candidate['intent_type'], now):
            return
        requested = state.followups.get(candidate['followup_id']) if candidate['followup_id'] else None
        if candidate['followup_id'] and requested is None:
            raise ValueError('跟进标识不存在')
        if requested and requested.get('item_id') and candidate['item_id'] not in (None, requested['item_id']):
            raise ValueError('不能改变已有跟进关联的待办')
        item_id = candidate['item_id'] or (requested.get('item_id') if requested else None)
        if item_id and not any(row.get('id') == item_id and row.get('type') == 'todo'
            and row.get('data', {}).get('status') == 'open' and row.get('trashed_at') is None for row in evidence.values()):
            return
        linked_check = next((row for row in state.checks.values() if item_id and row.get('item_id') == item_id
            and row.get('generation') == state.items[item_id].get('generation', 1)
            and (row['status'] not in TERMINAL or row['status'] == 'ignored')), None)
        if linked_check:
            return
        refs = candidate['evidence_refs']
        anchor = f"{item_id}:generation:{state.items[item_id].get('generation', 1)}" if item_id else next(
            (ref for ref in refs if evidence[ref].get('role') == 'user'), refs[0])
        fragment = 0 if item_id else candidate.get('commitment_index', 0)
        linked = next((row for row in state.followups.values() if item_id and row.get('item_id') == item_id
            and row.get('generation') == state.items[item_id].get('generation', 1) and row['status'] not in TERMINAL), None)
        record_id = linked['id'] if linked else candidate['followup_id'] or stable_id('pf_', tenant, user, anchor, fragment)
        existing = state.followups.get(record_id)
        if candidate['followup_id'] and existing is None:
            raise ValueError('跟进标识不存在')
        if existing:
            if existing['status'] in TERMINAL or existing['status'] == 'waiting':
                return
            # 已告知用户的相同证据只更新来源，不反复制造下一阶段。
            if now - existing.get('last_notified_at', 0) < 86400:
                return
            age_days = (now - existing['created_at']) / 86400
            stage = max(index for index, day in enumerate((0, 1, 3, 7, 14)) if age_days >= day)
            if set(refs).issubset(existing['evidence_refs']) and stage <= existing.get('review_stage', 0):
                return
        else:
            stage = 0
        proposed = absolute_time(candidate['proposed_time'])
        review = absolute_time(candidate['next_review_at'])
        reason = candidate['reason']
        body = ('根据已有记录，需要核实：' if candidate['certainty'] == 'explicit' else '可能需要跟进，供你确认：') + reason
        record = {**(existing or {}), 'id': record_id, 'version': existing['version'] + 1 if existing else 1,
            'kind': candidate['intent_type'], 'source': 'proactive_followup', 'review_stage': stage,
            'stable_key': existing.get('stable_key') or stable_id('anchor_', anchor, fragment) if existing else stable_id('anchor_', anchor, fragment),
            'title': candidate['title'], 'reason': reason, 'body': body, 'certainty': candidate['certainty'],
            'evidence_refs': refs, 'evidence_details': [{'ref': ref, 'text': evidence[ref].get('text', evidence[ref].get('data', {}).get('title', '')), 'at': evidence[ref].get('at')} for ref in refs], 'item_id': item_id, 'item_version': state.items[item_id]['version'] if item_id else None,
            'generation': state.items[item_id].get('generation', 1) if item_id else None,
            'status': 'awaiting_user', 'proposed_time': proposed,
            'next_review_at': max(now + 1800, review) if review else now + 86400,
            'suggested_action': candidate['suggested_action'], 'created_at': existing['created_at'] if existing else now,
            'updated_at': now}
        state.followups[record_id] = record
        self.notify(state, record, f"{record_id}:evidence:{fingerprint(refs)}:stage:{stage}", now)

    async def _failed(self, tenant: str, user: str, claim: dict[str, Any], error: str) -> None:
        """失败后仍保留消息游标，下一轮可重读同一证据。"""
        try:
            async with self.transaction(tenant, user) as state:
                scan = state.scan_state
                if scan.get('claim_token') == claim['token']:
                    attempts = scan.get('attempts', 0) + 1
                    scan.update(lease_until=0, claim_token=None, attempts=attempts,
                        last_error=error, next_sample_at=self.now() + min(1800, 60 * 5 ** min(attempts - 1, 2)))
        except PermissionDeniedError:
            logger.info('proactive_failure_identity_disabled')
