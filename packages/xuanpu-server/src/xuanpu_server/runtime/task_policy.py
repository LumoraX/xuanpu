"""后台工具的即时授权检查，覆盖脚本、文件、命令和连接器。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

import taifeng

from xuanpu_server.work_tasks.execution_models import ExecutionGrant
from xuanpu_server.work_tasks.execution_store import assert_lease

if TYPE_CHECKING:
    from xuanpu_server.work_tasks.execution_context import ExecutionBinding


class TaskPermissionPolicy(taifeng.PermissionPolicy):
    """不扩大原策略权限；每个工具先复查租约和原始授权。"""

    def __init__(self, inner: taifeng.PermissionPolicy, binding: ExecutionBinding) -> None:
        """绑定单次attempt。"""
        super().__init__(rules=list(inner.rules), default_mode=inner.default_mode,
            prompter=inner.prompter, prompter_timeout_seconds=inner.prompter_timeout_seconds,
            telemetry=inner.telemetry)
        self.binding = binding

    async def check(self, request: taifeng.PermissionRequest) -> taifeng.PermissionDecision:
        """身份撤销不等下一个heartbeat，也不允许已有批准绕过。"""
        from xuanpu_server.tenancy import Principal

        binding, lease = self.binding, self.binding.lease
        who = Principal(role='tenant_member', tenant_id=lease.tenant_id, user_id=lease.user_id, key_id=None)
        async with binding.store.workspace.transaction(who, 'work-task:exec:' + lease.task_id) as state:
            dispatch = assert_lease(state, lease, binding.at(state))
            grant = ExecutionGrant.model_validate_json(json.dumps(state.task_grants[dispatch.grant_id]))
            await binding.store.authorize(grant, binding.at(state))
            if request.scope in {'file_write', 'shell_exec', 'script_exec'}:
                import hashlib

                from xuanpu_server.work_tasks.store import require_task, save_record

                task = require_task(state, lease.task_id)
                target = request.target
                entry = {'scope': request.scope, 'attempt_no': lease.attempt_no,
                    'arguments_digest': hashlib.sha256(json.dumps(request.metadata, sort_keys=True,
                        default=str).encode()).hexdigest()}
                if request.scope == 'file_write' and target.startswith('/workspace/scratch/'):
                    entry['path'] = target.removeprefix('/workspace/')
                entries = (*task.resume_evidence, entry)
                if len(entries) > 128:
                    from xuanpu_server.errors import ConflictError

                    raise ConflictError('本地执行证据超过上限')
                save_record(state, task.model_copy(update={'unsafe_resume': True, 'resume_evidence': entries,
                    'state_version': task.state_version + 1, 'updated_at': max(binding.at(state), task.updated_at)}),
                    'local_state_intent')
        return await super().check(request)
