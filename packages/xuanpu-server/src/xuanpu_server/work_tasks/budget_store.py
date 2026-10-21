"""按实际请求保守预留，原子计量且跨attempt累计。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError
from xuanpu_server.quota import usage_day
from xuanpu_server.tenancy import Principal
from xuanpu_server.work_tasks.execution_models import BudgetReservation, ExecutionGrant
from xuanpu_server.work_tasks.execution_store import assert_lease, dispatch_of
from xuanpu_server.work_tasks.store import require_task

if TYPE_CHECKING:
    from xuanpu_server.work_tasks.execution_models import AttemptLease
    from xuanpu_server.work_tasks.execution_store import ExecutionStore
    from xuanpu_server.workspace.models import WorkspaceState


def usage_deltas(old: WorkspaceState, state: WorkspaceState) -> dict[str, tuple[int, int, int, int]]:
    """账本与用量同事务，不依赖根事件的异步尽力记账。"""
    deltas: dict[str, tuple[int, int, int, int]] = {}
    for key, value in state.task_reservations.items():
        if old.task_reservations.get(key) == value:
            continue
        after = BudgetReservation.model_validate_json(json.dumps(value))
        before_value = old.task_reservations.get(key)
        before = BudgetReservation.model_validate_json(json.dumps(before_value)) if before_value else None
        previous_input = 0 if before is None else (before.upper_bound if before.state == 'held' else before.input_tokens)
        after_input = after.upper_bound if after.state == 'held' else after.input_tokens
        amounts = (after_input - previous_input, after.output_tokens - (before.output_tokens if before else 0),
                   after.charged - (before.charged if before else 0), 0 if before else 1)
        current = deltas.get(after.day, (0, 0, 0, 0))
        deltas[after.day] = tuple(a + b for a, b in zip(current, amounts, strict=True))  # type: ignore[assignment]
    return deltas


class BudgetStore:
    """模型调用前提交完整上界；未知响应保持全部预留。"""

    def __init__(self, execution: ExecutionStore, *, max_task_tokens: int = 150000) -> None:
        """任务限额小于共享grant总额。"""
        self.execution, self.max_task_tokens = execution, max_task_tokens

    async def reserve(self, lease: AttemptLease, reservation_id: str, upper_bound: int,
                      *, now: float) -> BudgetReservation:
        """租户锁先于员工锁；数据库提交失败时调用方不得发模型请求。"""
        if type(upper_bound) is not int or upper_bound < 1:
            raise ConflictError('block_budget：输入上界未核定')
        workspace = self.execution.workspace
        who = Principal(role='tenant_member', tenant_id=lease.tenant_id, user_id=lease.user_id, key_id=None)
        async with workspace.transaction(who, 'work-task:budget:' + lease.task_id) as state:
            now = self.execution.transaction_time(state, now)
            dispatch = assert_lease(state, lease, now)
            if require_task(state,lease.task_id).unverified_write:
                raise ConflictError('block_unknown：未知写入前不能继续采样重试')
            if reservation_id in state.task_reservations:
                raise ConflictError('模型请求标识已经使用，不允许再次发送')
            grant = ExecutionGrant.model_validate_json(json.dumps(state.task_grants[dispatch.grant_id]))
            await self.execution.authorize(grant, now)
            entries = [BudgetReservation.model_validate_json(json.dumps(value)) for value in state.task_reservations.values()]
            task_used = sum(entry.charged for entry in entries if entry.task_id == lease.task_id)
            grant_used = sum(entry.charged for entry in entries if entry.grant_id == grant.grant_id)
            tenant = await workspace._control.get_tenant(lease.tenant_id)
            usage = await workspace._control.get_usage(lease.tenant_id, usage_day(now))
            if (tenant is None or task_used + upper_bound > self.max_task_tokens
                    or grant_used + upper_bound > grant.max_tokens
                    or (tenant.quota.max_daily_tokens is not None
                        and usage.total_tokens + upper_bound > tenant.quota.max_daily_tokens)):
                raise ConflictError('block_budget：剩余额度不足以覆盖完整请求上界')
            record = BudgetReservation(reservation_id=reservation_id, task_id=lease.task_id,
                grant_id=grant.grant_id, attempt_no=lease.attempt_no, day=usage_day(now),
                upper_bound=upper_bound, at=now)
            state.task_reservations[reservation_id] = record.model_dump(mode='json')
            return record

    async def settle(self, lease: AttemptLease, reservation_id: str, *, input_tokens: int,
                     output_tokens: int, now: float) -> None:
        """相同真实用量结算幂等；超上界回执阻止新调用并保留证据。"""
        workspace = self.execution.workspace
        who = Principal(role='tenant_member', tenant_id=lease.tenant_id, user_id=lease.user_id, key_id=None)
        async with workspace.transaction(who, 'work-task:budget:' + lease.task_id) as state:
            now = self.execution.transaction_time(state, now)
            assert_lease(state, lease, now)
            saved = BudgetReservation.model_validate_json(json.dumps(state.task_reservations[reservation_id]))
            if saved.task_id != lease.task_id or saved.attempt_no != lease.attempt_no:
                raise ConflictError('用量回执归属不匹配')
            if saved.state == 'settled':
                if (saved.input_tokens, saved.output_tokens) != (input_tokens, output_tokens):
                    raise ConflictError('用量回执不一致')
                return
            if min(input_tokens, output_tokens) < 0:
                raise ConflictError('用量不能为负')
            state.task_reservations[reservation_id] = saved.model_copy(update={
                'state': 'settled', 'input_tokens': input_tokens, 'output_tokens': output_tokens}).model_dump(mode='json')
            if input_tokens + output_tokens > saved.upper_bound:
                dispatch = dispatch_of(state, lease.task_id)
                state.task_dispatch[lease.task_id] = dispatch.model_copy(update={
                    'block_reason': 'model_bound_violated'}).model_dump(mode='json')
                state.task_grants[saved.grant_id]['revoked'] = True
