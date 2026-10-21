"""任务会话原批准续接的最后闸门，阻止raw Resume绕过效应屏障。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import ConflictError
from xuanpu_server.work_tasks.execution_context import current_execution
from xuanpu_server.work_tasks.store import _lifecycle_guard, require_task

if TYPE_CHECKING:
    from xuanpu_server.controlplane.models import SessionRecord
    from xuanpu_server.controlplane.store import ControlPlaneStore


async def require_safe_resume(store: ControlPlaneStore, record: SessionRecord,
                              resolutions: dict[str, Any]) -> None:
    """旧R1保持原契约；R2需要真实派发租约且不接受文字核实或重试未知写入。"""
    if record.work_task_id is None or record.user_id is None:
        return
    async with store.workspace_store.transaction(record.tenant_id, record.user_id, _lifecycle_guard,
            'work-task:one:' + record.work_task_id) as state:
        task = require_task(state, record.work_task_id)
    if task.recovery_policy_version != 2:
        return
    binding = current_execution.get()
    if binding is None or binding.lease.task_id != task.task_id:
        raise ConflictError('R2任务批准只能由持久执行器推进')
    if task.unverified_write or any(isinstance(value, dict) and value.get('action') in {'retry', 'provide'}
                                    for value in resolutions.values()):
        raise ConflictError('未知写入须先由权威适配器核实，不能重发或用文字回填')
