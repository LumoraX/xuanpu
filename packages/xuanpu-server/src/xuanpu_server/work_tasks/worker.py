"""Registry持有的唯一持久派发器，不另建竞争执行任务。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    QuotaExceededError,
)
from xuanpu_server.work_tasks.execution_context import ExecutionBinding, current_execution
from xuanpu_server.work_tasks.execution_models import AttemptLease, ExecutionGrant
from xuanpu_server.work_tasks.execution_store import ExecutionStore, dispatch_of
from xuanpu_server.work_tasks.models import TERMINAL
from xuanpu_server.work_tasks.recovery import RecoveryFacts, choose_recovery
from xuanpu_server.work_tasks.store import _lifecycle_guard

if TYPE_CHECKING:
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.work_tasks.config import DurableExecutionConfig
    from xuanpu_server.work_tasks.decisions import TaskDecision
    from xuanpu_server.work_tasks.models import WorkTaskRecord
    from xuanpu_server.work_tasks.registry import TaskRegistry

logger = logging.getLogger(__name__)


class DurableWorker:
    """持久状态负责派发，Registry强引用负责当前进程的执行责任。"""

    def __init__(self, registry: TaskRegistry, config: DurableExecutionConfig) -> None:
        """配置随平台注入；即使开关关闭也能查询并保守处理既有R2记录。"""
        self.registry, self.config = registry, config
        self.execution = ExecutionStore(registry.store.workspace, lease_seconds=config.lease_seconds)
        self.execution.verified_text_bounds = config.verified_text_bounds
        self.execution.max_attempts = config.max_attempts
        self.execution.database_clock = registry.now is time.time
        self.cursor: tuple[str, str] | None = None

    async def scan(self) -> None:
        """有界公平唤醒，不在没有名额时认领或消耗attempt。"""
        if not self.config.enabled or not self.registry._accepting or len(self.registry._active) >= 16:
            return
        store = self.execution.workspace.store
        rows = await store.durable_candidates(after=self.cursor, limit=100, now=self.registry.now())
        if not rows and self.cursor is not None:
            self.cursor = None
            rows = await store.durable_candidates(limit=100, now=self.registry.now())
        for tenant, user, identifier in rows:
            if len(self.registry._active) >= 16:
                break
            self.cursor = tenant, user
            task = await self.registry.store.lifecycle_task(tenant, user, identifier)
            if task.cancel_requested:
                from xuanpu_server.tenancy import Principal

                await self.registry.cancel(Principal(role='tenant_member',tenant_id=tenant,user_id=user,key_id=None),task)
                continue
            if task.grant_id is None:
                raise ConflictError('R2任务缺少执行授权')
            async with self.execution.workspace.store.transaction(tenant, user, _lifecycle_guard,
                    'work-task:exec:' + identifier, allow_inactive=True) as state:
                grant = ExecutionGrant.model_validate_json(json.dumps(state.task_grants[task.grant_id]))
            self.registry.start(grant.principal(), task)

    async def _claim_or_settle(self, who: Principal, task: WorkTaskRecord) -> AttemptLease | None:
        """没有名额不认领，授权或次数阻塞在资源启动前结束。"""
        registry = self.registry
        key = task.tenant_id, task.user_id, task.task_id
        if not self.config.enabled or (not task.frozen_staging and not await registry._admit(key)):
            return None
        try:
            lease = await self.execution.claim(who, task.task_id, owner=registry.process_owner, now=registry.now())
        except PermissionDeniedError:
            await self.execution.block(task, reason='grant_revoked', now=registry.now())
            registry._executing.discard(key)
            return None
        if lease is None:
            dispatch = await self.execution.get(who, task.task_id)
            if dispatch.block_reason == 'attempts_exhausted':
                current = await registry.store.get(who, task.task_id)
                if registry.cleanup is not None:
                    await registry.cleanup(current)
                await registry._settle(current, 'failed', 'attempts_exhausted')
            registry._executing.discard(key)
            return None
        return lease

    async def execute(self, who: Principal, task: WorkTaskRecord, decision: TaskDecision | None) -> None:
        """准入先于认领；等待人和上传阶段不占模型运行时长预算。"""
        registry = self.registry
        key = task.tenant_id, task.user_id, task.task_id
        lease = await self._claim_or_settle(who,task)
        if lease is None:
            return
        task = task.model_copy(update={'process_owner':lease.owner,'attempt_no':lease.attempt_no,'resources_pending':True})
        binding = ExecutionBinding(lease, self.execution, registry.now)
        token = current_execution.set(binding)
        heartbeat = asyncio.create_task(self._heartbeat(binding, asyncio.current_task()))
        try:
            task = await registry.store.get(who, task.task_id)
            if decision is None and task.pending_decision:
                from pydantic import TypeAdapter

                from .decisions import TaskDecision

                decision = TypeAdapter(TaskDecision).validate_json(json.dumps(task.pending_decision))
            if task.status != 'running':
                task = await registry.store.transition(who, task.task_id, 'running',
                    expected_version=task.state_version, at=registry.now(), event_kind='attempt_started')
            async with asyncio.timeout(self.config.attempt_seconds):
                result = dict(await registry.runner.run(who, task, decision))
            await registry._finish_result(who, task, result)
            current = await registry.store.get(who, task.task_id)
            target = 'waiting_user' if current.status == 'waiting_user' else 'settled'
            await self.execution.release(lease, state_name=target, now=registry.now())
        except asyncio.CancelledError:
            await self._cancelled(task, lease, binding.stop_reason)
            raise
        except PermissionDeniedError:
            await registry._settle(task, 'interrupted', 'grant_revoked')
            await self.execution.release(lease, state_name='blocked', now=registry.now(), block_reason='grant_revoked')
        except Exception as error:
            await self._failed(task, lease, error)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            current_execution.reset(token)
            registry._executing.discard(key)

    async def _heartbeat(self, binding: ExecutionBinding, parent: asyncio.Task[Any] | None) -> None:
        """失去租约或授权立即取消唯一执行者，不能让旧网络结果继续提交。"""
        while True:
            await asyncio.sleep(self.config.renew_seconds)
            try:
                binding.lease = await self.execution.renew(binding.lease, now=self.registry.now())
            except Exception as error:
                binding.stop_reason = 'grant_revoked' if isinstance(error, PermissionDeniedError) else 'lease_lost'
                if parent is not None:
                    parent.cancel()
                raise

    async def _cancelled(self, task: WorkTaskRecord, lease: AttemptLease, stop_reason: str | None) -> None:
        """正常关闭保留非终态；用户取消和未知写入仍结算原业务终态。"""
        registry = self.registry
        current = (await registry.store.lifecycle_records(task.tenant_id, task.user_id))
        record = next((entry for entry in current if entry.task_id == task.task_id), task)
        if record.unverified_write:
            await registry._settle(task, 'outcome_unknown', 'write_outcome_unverified', lease=lease)
            target = 'settled'
        elif record.cancel_requested:
            await registry._settle(task, 'cancelled', 'cancel_requested', lease=lease)
            target = 'settled'
        elif stop_reason is not None:
            await self.execution.release(lease, state_name='blocked', now=registry.now(), block_reason=stop_reason)
            return
        elif registry._closing and not record.cancel_requested:
            target = 'paused_shutdown'
        else:
            await registry._settle(task, 'cancelled', 'cancel_requested', lease=lease)
            target = 'settled'
        await self.execution.release(lease, state_name=target, now=registry.now())

    async def _failed(self, task: WorkTaskRecord, lease: AttemptLease, error: Exception) -> None:
        """仅无写入屏障且资源已证实退出才允许有限退避重算。"""
        registry = self.registry
        records = await registry.store.lifecycle_records(task.tenant_id, task.user_id)
        current = next((entry for entry in records if entry.task_id == task.task_id), task)
        if current.unverified_write:
            await registry._settle(task, 'outcome_unknown', 'write_outcome_unverified', lease=lease)
            target, reason = 'settled', 'write_outcome_unverified'
        elif current.frozen_staging:
            target, reason = 'blocked', 'staging_unverified'
        elif isinstance(error, (InvalidRequestError, NotFoundError)):
            await registry._settle(task, 'failed', 'source_or_output_invalid', lease=lease)
            target, reason = 'settled', 'source_or_output_invalid'
        elif isinstance(error, QuotaExceededError) and error.message.startswith('进行中的对话数'):
            target, reason = 'retry_wait', 'turn_capacity_busy'
        elif isinstance(error, QuotaExceededError) or (isinstance(error, ConflictError) and 'block_budget' in error.message):
            target, reason = 'blocked', 'block_budget'
        elif current.resources_pending:
            target, reason = 'blocked', 'cleanup_unverified'
        elif current.attempt_no >= self.config.max_attempts:
            await registry._settle(task, 'failed', 'attempts_exhausted', lease=lease)
            target, reason = 'settled', 'attempts_exhausted'
        else:
            target, reason = 'retry_wait', type(error).__name__
        await self.execution.release(lease, state_name=target, now=registry.now(), block_reason=reason,
            due_at=registry.now() + (30. if lease.attempt_no == 1 else 120.))
        logger.warning('持久任务停止 task=%s reason=%s', task.task_id, reason)

    async def reconcile(self, task: WorkTaskRecord, *, restart: bool) -> None:
        """已知资源先退出，再依据授权、屏障和冻结检查点选择恢复阶段。"""
        registry, workspace = self.registry, self.execution.workspace
        key = task.tenant_id, task.user_id, task.task_id
        if key in registry._active:
            return
        if restart and task.resources_pending and registry.recover is not None:
            await registry._recover(task)
        task = await registry.store.lifecycle_task(task.tenant_id,task.user_id,task.task_id)
        if task.cancel_requested and task.status not in TERMINAL:
            if registry.cleanup is not None:
                await registry.cleanup(task)
            await registry._settle(task,'cancelled','cancel_requested')
            settled = await registry.store.lifecycle_task(task.tenant_id,task.user_id,task.task_id)
            await self.execution.settle_terminal(settled,now=registry.now())
            return
        async with workspace.store.transaction(task.tenant_id, task.user_id, _lifecycle_guard,
                'work-task:exec:' + task.task_id, allow_inactive=True) as state:
            from xuanpu_server.work_tasks.store import require_task

            task = require_task(state, task.task_id)
            dispatch = dispatch_of(state, task.task_id)
            grant = ExecutionGrant.model_validate_json(json.dumps(state.task_grants[dispatch.grant_id]))
            valid = self.config.enabled and registry.now() < task.accepted_at + self.config.recovery_seconds
            auth_reason = 'feature_disabled' if not self.config.enabled else 'recovery_window_expired'
            try:
                await self.execution.authorize(grant, registry.now())
            except PermissionDeniedError:
                valid = False
                auth_reason = 'grant_revoked'
            effects = [entry for entry in state.task_effects.values() if entry['task_id'] == task.task_id]
            safe_pending = bool(task.pending) and not task.inputs and not task.unsafe_resume and all(
                entry.get('detail', {}).get('target') in self.config.stateless_http_tools for entry in task.pending)
            choice = choose_recovery(RecoveryFacts(terminal=task.status in TERMINAL,
                resources_pending=task.resources_pending, grant_valid=valid,
                unknown_effect=task.unverified_write or any(e['state'] == 'dispatch_intent' for e in effects),
                any_effect=bool(effects), frozen=dispatch.checkpoint.phase == 'artifacts_staged',
                waiting=bool(task.pending) and not task.outputs_verified,
                pending_http_safe=safe_pending, attempts_exhausted=grant.attempts_used >= grant.max_attempts or task.attempt_no >= min(self.config.max_attempts,grant.task_attempt_limit)))
            attempt = state.task_attempts.get(f'{task.task_id}:{dispatch.attempt_no}')
            from .recovery import dispatch_plan

            target,reason,due_at = dispatch_plan(task,dispatch,choice,auth_reason=auth_reason,
                now=registry.now(),restart=restart,started_at=attempt['started_at'] if attempt else None)
            if restart or dispatch.state in {'paused_shutdown'}:
                state.task_dispatch[task.task_id] = dispatch.model_copy(update={'state': target,
                    'block_reason': reason, 'due_at': due_at, 'expires_at': 0.0}).model_dump(mode='json')
        if choice == 'block_unknown':
            await registry._settle(task, 'outcome_unknown', 'write_outcome_unverified')
        elif choice == 'fail_attempts':
            await registry._settle(task, 'failed', 'attempts_exhausted')
        if (registry._accepting and target == 'ready' and self.config.enabled
                and (restart or dispatch.state in {'ready', 'retry_wait', 'paused_shutdown'})
                and dispatch.due_at <= registry.now()):
            registry.start(grant.principal(), task)
