"""员工事务内的持久授权、认领、租约和阶段推进。"""
from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
)
from xuanpu_server.tenancy import Principal
from xuanpu_server.work_tasks.execution_models import (
    AttemptLease,
    Checkpoint,
    DispatchRecord,
    ExecutionGrant,
    TaskAttempt,
)
from xuanpu_server.work_tasks.models import TERMINAL
from xuanpu_server.work_tasks.store import require_task, save_record, validate_sources

if TYPE_CHECKING:
    from xuanpu_server.runtime.effect_receipts import ReceiptVerifier
    from xuanpu_server.work_tasks.models import WorkTaskRecord
    from xuanpu_server.workspace.models import WorkspaceState
    from xuanpu_server.workspace.service import WorkspaceService


def initialize(state: WorkspaceState, task: WorkTaskRecord, who: Principal, *,
               max_attempts: int = 3, recovery_seconds: float = 1800.) -> WorkTaskRecord:
    """任务、独立授权和派发记录在同一事务受理。"""
    grant = ExecutionGrant(grant_id=task.grant_id or 'grant_' + secrets.token_hex(12),
        tenant_id=task.tenant_id, user_id=task.user_id, actor=who.actor, role='tenant_admin' if who.role == 'tenant_admin' else 'tenant_member',
        key_id=who.key_id, package_digest=task.package_digest,
        input_digests=tuple(source.sha256 for source in task.inputs),
        created_at=task.accepted_at, expires_at=task.accepted_at + recovery_seconds, max_attempts=max_attempts)
    if task.grant_id is not None:
        existing = state.task_grants.get(task.grant_id)
        if existing is None:
            raise NotFoundError('引用的执行授权不存在')
        grant = ExecutionGrant.model_validate_json(json.dumps(existing))
        inputs_valid = (set(source.item_id for source in task.inputs).issubset(grant.input_file_ids)
            if grant.input_policy=='latest_at_occurrence' else
            set(source.sha256 for source in task.inputs).issubset(grant.input_digests))
        if (not grant.accepting_new_tasks or grant.actor != who.actor or grant.package_digest != task.package_digest
                or not inputs_valid):
            raise ConflictError('任务超出原执行授权范围')
    dispatch = DispatchRecord(task_id=task.task_id, grant_id=grant.grant_id,
        due_at=task.accepted_at, checkpoint=Checkpoint(at=task.accepted_at))
    state.task_grants[grant.grant_id] = grant.model_dump(mode='json')
    state.task_dispatch[task.task_id] = dispatch.model_dump(mode='json')
    return task.model_copy(update={'grant_id': grant.grant_id})


def dispatch_of(state: WorkspaceState, task_id: str) -> DispatchRecord:
    """只在本人事务快照中解析持久派发。"""
    value = state.task_dispatch.get(task_id)
    if value is None:
        raise NotFoundError('持久派发不存在')
    return DispatchRecord.model_validate_json(json.dumps(value))


def assert_lease(state: WorkspaceState, lease: AttemptLease, now: float) -> DispatchRecord:
    """过期租约永不能复活；所有旧owner的提交均拒绝。"""
    dispatch = dispatch_of(state, lease.task_id)
    if (dispatch.state != 'leased' or dispatch.owner != lease.owner or dispatch.epoch != lease.epoch
            or dispatch.attempt_no != lease.attempt_no or dispatch.expires_at <= now):
        raise ConflictError('stale_epoch：执行所有权已失效')
    return dispatch


def definition_ready(state: WorkspaceState,task: WorkTaskRecord,dispatch: DispatchRecord) -> bool:
    """暂停仅阻止新认领，已经取得的租约仍可按原策略完成收尾。"""
    if task.definition_id is None:
        return True
    definition=state.task_definitions.get(task.definition_id)
    if definition is not None and definition['enabled']:
        return True
    state.task_dispatch[task.task_id]=dispatch.model_copy(update={
        'state':'blocked','block_reason':'definition_paused'}).model_dump(mode='json')
    return False


def live_attempt(task: WorkTaskRecord, dispatch: DispatchRecord, now: float) -> bool:
    """仍有效或资源尚未清理的执行不能由重复扫描更改。"""
    return dispatch.state == 'leased' and (dispatch.expires_at > now or task.resources_pending)


class ExecutionStore:
    """复用既有双后端员工事务，不新增队列或第二个执行所有者。"""

    def __init__(self, workspace: WorkspaceService, *, lease_seconds: float = 60.) -> None:
        """注入身份、存储与租约有效期。"""
        self.workspace, self.lease_seconds = workspace, lease_seconds
        self.verified_text_bounds = False
        self.max_attempts = 3
        self.database_clock = False
        self.effect_verifiers: dict[str, ReceiptVerifier] = {}

    def transaction_time(self, state: WorkspaceState, supplied: float) -> float:
        """生产PG以数据库时钟判定租约，测试显式注入时钟仍可复现。"""
        return state.transaction_at if self.database_clock and state.transaction_at is not None else supplied

    async def create_grant(self, who: Principal, grant: ExecutionGrant) -> ExecutionGrant:
        """独立保存明确授权，同标识必须完全一致。"""
        tenant, user = await self.workspace.require_employee(who)
        if (grant.tenant_id, grant.user_id, grant.actor) != (tenant, user, who.actor):
            raise PermissionDeniedError('授权身份不匹配')
        async with self.workspace.transaction(who, 'work-task:grant:' + grant.grant_id) as state:
            old = state.task_grants.get(grant.grant_id)
            if old is not None and old != grant.model_dump(mode='json'):
                raise ConflictError('原执行授权不能改写')
            state.task_grants[grant.grant_id] = grant.model_dump(mode='json')
        return grant

    async def get(self, who: Principal, task_id: str) -> DispatchRecord:
        """本人可查派发；公共响应不直接序列化此内部记录。"""
        async with self.workspace.transaction(who, 'work-task:exec:' + task_id) as state:
            require_task(state, task_id)
            return dispatch_of(state, task_id)

    async def grant(self, who: Principal, grant_id: str) -> ExecutionGrant:
        """独立授权按员工隔离读取。"""
        async with self.workspace.transaction(who, 'work-task:grant:' + grant_id) as state:
            if grant_id not in state.task_grants:
                raise NotFoundError('执行授权不存在')
            return ExecutionGrant.model_validate_json(json.dumps(state.task_grants[grant_id]))

    async def authorize(self, grant: ExecutionGrant, now: float) -> Principal:
        """执行前复查原key、员工当前角色以及租户状态。"""
        who = grant.principal()
        await self.workspace.require_employee(who)
        user = await self.workspace._identity.get_user(grant.tenant_id, grant.user_id)
        if grant.revoked or now >= grant.expires_at or user is None:
            raise PermissionDeniedError('grant_revoked')
        if grant.role == 'tenant_admin' and user.role != 'tenant_admin':
            raise PermissionDeniedError('grant_role_reduced')
        if grant.key_id is not None:
            key = await self.workspace._control.get_api_key(grant.key_id)
            if (key is None or not key.active or not key.can_act_as_user
                    or key.tenant_id != grant.tenant_id
                    or (grant.role == 'tenant_admin' and key.role != 'tenant_admin')):
                raise PermissionDeniedError('grant_revoked')
        return who

    async def claim(self, who: Principal, task_id: str, *, owner: str, now: float) -> AttemptLease | None:
        """原子认领，旧资源未清理时绝不创建新attempt。"""
        async with self.workspace.transaction(who, 'work-task:exec:' + task_id) as state:
            now = self.transaction_time(state, now)
            task, dispatch = require_task(state, task_id), dispatch_of(state, task_id)
            grant = ExecutionGrant.model_validate_json(json.dumps(state.task_grants[dispatch.grant_id]))
            await self.authorize(grant, now)
            if task.status in TERMINAL or task.cancel_requested or dispatch.due_at > now:
                return None
            if live_attempt(task, dispatch, now):
                return None
            if not definition_ready(state,task,dispatch):
                return None
            try:
                validate_sources(state, task)
            except (ConflictError, InvalidRequestError, NotFoundError):
                state.task_dispatch[task_id] = dispatch.model_copy(update={
                    'state': 'blocked', 'block_reason': 'source_unavailable'}).model_dump(mode='json')
                return None
            if dispatch.state in {'blocked', 'waiting_user', 'settled'}:
                return None
            effects = [entry for entry in state.task_effects.values() if entry['task_id'] == task_id]
            if task.unverified_write or (effects and not task.frozen_staging and not task.pending_decision):
                state.task_dispatch[task_id] = dispatch.model_copy(update={
                    'state': 'blocked', 'block_reason': 'block_unknown' if task.unverified_write else 'block_resume'}).model_dump(mode='json')
                return None
            if not task.frozen_staging and (grant.attempts_used >= grant.max_attempts or task.attempt_no >= min(self.max_attempts,grant.task_attempt_limit)):
                state.task_dispatch[task_id] = dispatch.model_copy(update={
                    'state': 'blocked', 'block_reason': 'attempts_exhausted'}).model_dump(mode='json')
                return None
            active = [entry for entry in state.execution_active if
                (entry['tenant_id'], entry['user_id'], entry['task_id']) != (task.tenant_id, task.user_id, task_id)
                and (entry['expires_at'] > now or entry['resources_pending'])
                and entry.get('checkpoint', {}).get('phase') != 'artifacts_staged']
            tenant = await self.workspace._control.get_tenant(task.tenant_id)
            if not task.frozen_staging and (len(active) >= 8 or any((entry['tenant_id'], entry['user_id']) == (task.tenant_id, task.user_id)
                    for entry in active) or tenant is None
                    or sum(entry['tenant_id'] == task.tenant_id for entry in active)
                       >= min(8, tenant.quota.max_concurrent_turns or 8)):
                return None
            attempt_no, epoch = dispatch.attempt_no + 1, dispatch.epoch + 1
            lease = AttemptLease(task.tenant_id, task.user_id, task_id, attempt_no, owner, epoch,
                                 now + self.lease_seconds)
            updated = dispatch.model_copy(update={'state': 'leased', 'owner': owner, 'epoch': epoch,
                'attempt_no': attempt_no, 'expires_at': lease.expires_at, 'block_reason': None})
            state.task_dispatch[task_id] = updated.model_dump(mode='json')
            state.task_grants[grant.grant_id] = grant.model_copy(update={
                'attempts_used': grant.attempts_used + (0 if task.frozen_staging else 1)}).model_dump(mode='json')
            attempt = TaskAttempt(task_id=task_id, attempt_no=attempt_no, owner=owner, epoch=epoch,
                started_at=now, expires_at=lease.expires_at, scope_id=f'task:{task_id}:attempt:{attempt_no}')
            state.task_attempts[f'{task_id}:{attempt_no}'] = attempt.model_dump(mode='json')
            previous = state.task_attempts.get(f'{task_id}:{task.attempt_no}')
            if previous is not None and previous['attempt_no'] != attempt_no:
                previous['result_receipt'] = task.result_receipt
                previous['resume_evidence'] = list(task.resume_evidence)
            reset = ({'result_receipt':{},'result_summary':'','unsafe_resume':False,'resume_evidence':(),
                      'output_hashes':{}} if not task.pending and not task.frozen_staging else {})
            # 业务状态仍由Registry推进；这里先持久承担资源清理责任。
            task = task.model_copy(update={**reset,'attempt_no': attempt_no, 'process_owner': owner,
                'resources_pending': True, 'resources_recovered': False,
                'state_version': task.state_version + 1, 'updated_at': max(now, task.updated_at)})
            save_record(state, task, 'attempt_claimed')
            return lease

    async def renew(self, lease: AttemptLease, *, now: float) -> AttemptLease:
        """严格未过期比较，续约不改变epoch。"""
        who = Principal(role='tenant_member', tenant_id=lease.tenant_id, user_id=lease.user_id, key_id=None)
        async with self.workspace.transaction(who, 'work-task:exec:' + lease.task_id) as state:
            now = self.transaction_time(state, now)
            dispatch = assert_lease(state, lease, now)
            grant = ExecutionGrant.model_validate_json(json.dumps(state.task_grants[dispatch.grant_id]))
            await self.authorize(grant, now)
            expires = now + self.lease_seconds
            state.task_dispatch[lease.task_id] = dispatch.model_copy(update={'expires_at': expires}).model_dump(mode='json')
            state.task_attempts[f'{lease.task_id}:{lease.attempt_no}']['expires_at'] = expires
            return AttemptLease(lease.tenant_id, lease.user_id, lease.task_id, lease.attempt_no,
                                lease.owner, lease.epoch, expires)

    async def checkpoint(self, lease: AttemptLease, phase: str, evidence: dict[str, Any] | None = None,
                         *, now: float, task_changes: dict[str, Any] | None = None) -> None:
        """阶段与业务进展同事务保存，证据正文不进入管理审计。"""
        who = Principal(role='tenant_member', tenant_id=lease.tenant_id, user_id=lease.user_id, key_id=None)
        if len(json.dumps(evidence or {}).encode()) > 65536:
            raise ConflictError('阶段证据超过上限')
        async with self.workspace.transaction(who, 'work-task:exec:' + lease.task_id) as state:
            now = self.transaction_time(state, now)
            dispatch = assert_lease(state, lease, now)
            checkpoint = Checkpoint(phase=phase, evidence=evidence or {}, at=now)
            state.task_dispatch[lease.task_id] = dispatch.model_copy(update={
                'checkpoint': checkpoint}).model_dump(mode='json')
            state.task_attempts[f'{lease.task_id}:{lease.attempt_no}']['phase'] = phase
            task = require_task(state, lease.task_id)
            save_record(state, task.model_copy(update={**(task_changes or {}), 'state_version': task.state_version + 1,
                        'updated_at': max(now, task.updated_at)}), 'checkpoint_' + phase)

    async def settle_terminal(self, task: WorkTaskRecord, *, now: float) -> None:
        """持久取消等终态的派发收尾；不能据旧记录覆盖新attempt。"""
        from .store import _lifecycle_guard

        async with self.workspace.store.transaction(task.tenant_id, task.user_id, _lifecycle_guard,
                'work-task:exec:' + task.task_id, allow_inactive=True) as state:
            current = require_task(state, task.task_id)
            if current.status not in TERMINAL:
                raise ConflictError('派发只可按当前终态结清')
            if (current.process_owner,current.attempt_no) != (task.process_owner,task.attempt_no):
                raise ConflictError('旧attempt不能结清新派发')
            dispatch = dispatch_of(state,task.task_id)
            state.task_dispatch[task.task_id] = dispatch.model_copy(update={
                'state':'settled','expires_at':0.0,'due_at':now}).model_dump(mode='json')

    async def revoke_employee(self, tenant: str, user: str) -> None:
        """身份停用流水线内部撤销全部执行授权，不提供内容读取入口。"""
        from .store import _lifecycle_guard

        async with self.workspace.store.transaction(tenant, user, _lifecycle_guard,
                'work-task:revoke', allow_inactive=True) as state:
            for grant in state.task_grants.values():
                grant['revoked'] = True

    async def block(self, task: WorkTaskRecord, *, reason: str, now: float) -> None:
        """没有认领成功也能内部保留阻塞原因，不产生新attempt。"""
        from .store import _lifecycle_guard

        async with self.workspace.store.transaction(task.tenant_id, task.user_id, _lifecycle_guard,
                'work-task:exec:' + task.task_id, allow_inactive=True) as state:
            dispatch = dispatch_of(state, task.task_id)
            if dispatch.state == 'leased' and dispatch.expires_at > now:
                raise ConflictError('活跃执行不能由未认领者改写')
            state.task_dispatch[task.task_id] = dispatch.model_copy(update={
                'state': 'blocked', 'block_reason': reason}).model_dump(mode='json')

    async def release(self, lease: AttemptLease, *, state_name: str, now: float,
                      block_reason: str | None = None, due_at: float | None = None) -> None:
        """清理后的派发交接只允许原epoch；业务终态不回退。"""
        from .store import _lifecycle_guard

        async with self.workspace.store.transaction(lease.tenant_id, lease.user_id, _lifecycle_guard,
                'work-task:exec:' + lease.task_id, allow_inactive=True) as state:
            dispatch = dispatch_of(state, lease.task_id)
            if (dispatch.owner, dispatch.epoch) != (lease.owner, lease.epoch):
                raise ConflictError('stale_epoch：旧执行不能移交新派发')
            task=require_task(state,lease.task_id)
            if task.status in TERMINAL and not task.resources_pending and not task.artifact_recovery_pending:
                state_name,block_reason='settled',None
            state.task_dispatch[lease.task_id] = DispatchRecord.model_validate({**dispatch.model_dump(),
                'state': state_name, 'block_reason': block_reason, 'due_at': due_at if due_at is not None else now,
                'expires_at': 0.0, 'changed_at':now}).model_dump(mode='json')
            attempt = state.task_attempts.get(f'{lease.task_id}:{lease.attempt_no}')
            if attempt is not None:
                attempt['finished_at'] = now
                task = require_task(state,lease.task_id)
                attempt['result_receipt'] = task.result_receipt
                attempt['resume_evidence'] = list(task.resume_evidence)
