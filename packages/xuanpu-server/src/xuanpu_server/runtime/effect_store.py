"""与执行epoch同事务保存写入意图和确定回执。"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError
from xuanpu_server.runtime.effect_receipts import EffectReceipt
from xuanpu_server.tenancy import Principal
from xuanpu_server.work_tasks.execution_store import assert_lease
from xuanpu_server.work_tasks.store import require_task, save_record

if TYPE_CHECKING:
    from xuanpu_server.work_tasks.execution_context import ExecutionBinding


class EffectStore:
    """所有传输在派发前建立统一屏障，不依赖审计异步落库。"""

    def __init__(self, binding: ExecutionBinding) -> None:
        """绑定当前attempt，跨epoch结果只能核实不能推进。"""
        self.binding = binding

    def _who(self) -> Principal:
        """只构造该任务员工身份，原始grant另外复核。"""
        lease = self.binding.lease
        return Principal(role='tenant_member', tenant_id=lease.tenant_id, user_id=lease.user_id, key_id=None)

    async def begin(self, *, call_id: str, tool: str, arguments_digest: str,
                    submission_id: str | None = None) -> EffectReceipt:
        """提交成功后才能发网络；相同逻辑调用存在即拒绝再次派发。"""
        binding, lease = self.binding, self.binding.lease
        identifier = hashlib.sha256((lease.task_id + ':' + tool + ':' + call_id).encode()).hexdigest()
        async with binding.store.workspace.transaction(self._who(), 'work-task:exec:' + lease.task_id) as state:
            dispatch = assert_lease(state, lease, binding.at(state))
            from xuanpu_server.work_tasks.execution_models import ExecutionGrant

            grant = ExecutionGrant.model_validate_json(json.dumps(state.task_grants[dispatch.grant_id]))
            await binding.store.authorize(grant, binding.at(state))
            task = require_task(state, lease.task_id)
            if task.execution_kind == 'capability':
                raise ConflictError('首期通用能力仅允许只读连接器，不能执行外部写入')
            if task.cancel_requested:
                raise ConflictError('任务已取消，禁止新外部派发')
            if task.unverified_write:
                raise ConflictError('已有未知写入，禁止派发另一外部动作')
            if identifier in state.task_effects:
                raise ConflictError('效应意图已经存在，禁止重发')
            record = EffectReceipt(logical_action_id=identifier, task_id=lease.task_id,
                attempt_no=lease.attempt_no, call_id=call_id, submission_id=submission_id,
                tool=tool, arguments_digest=arguments_digest, created_at=binding.now())
            state.task_effects[identifier] = record.model_dump(mode='json')
            calls = tuple(sorted(set(task.unverified_calls) | {call_id}))
            save_record(state, task.model_copy(update={'unverified_write': True, 'unverified_calls': calls,
                'state_version': task.state_version + 1, 'updated_at': max(binding.now(), task.updated_at)}), 'effect_dispatch_intent')
            return record

    async def confirm(self, receipt: EffectReceipt, text: str, *, authority: str) -> None:
        """仅确定成功的传输返回才核实；HTTP错误和断连不能解除屏障。"""
        if not authority:
            raise ConflictError('缺少权威下游成功凭据')
        binding, lease = self.binding, self.binding.lease
        async with binding.store.workspace.transaction(self._who(), 'work-task:exec:' + lease.task_id) as state:
            assert_lease(state, lease, binding.at(state))
            current = EffectReceipt.model_validate_json(json.dumps(state.task_effects[receipt.logical_action_id]))
            state.task_effects[receipt.logical_action_id] = current.model_copy(update={
                'state': 'confirmed_success', 'result_digest': hashlib.sha256(text.encode()).hexdigest(),
                'authority': authority}).model_dump(mode='json')
            task = require_task(state, lease.task_id)
            remaining = tuple(call for call in task.unverified_calls if call != receipt.call_id)
            save_record(state, task.model_copy(update={'unverified_write': bool(remaining),
                'unverified_calls': remaining, 'state_version': task.state_version + 1,
                'updated_at': max(binding.now(), task.updated_at)}), 'effect_confirmed')
