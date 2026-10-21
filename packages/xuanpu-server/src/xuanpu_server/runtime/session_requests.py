"""稳定会话请求与服务端任务关联的验证。"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, InvalidRequestError
from xuanpu_server.runtime.execution_scope import ExecutionScope

if TYPE_CHECKING:
    from xuanpu_server.controlplane.models import SessionRecord
    from xuanpu_server.tenancy import Principal


def stable_session_id(principal: Principal, request_id: str) -> str:
    """同员工同请求有稳定会话标识，不接受无员工身份或无效标识。"""
    if not principal.user_id or not request_id.strip() or len(request_id) > 128:
        raise InvalidRequestError("幂等会话需要员工和有效请求标识")
    raw = json.dumps([principal.require_tenant(), principal.user_id, request_id])
    return "ses_" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def check_request(
    record: SessionRecord, principal: Principal, package: str, version: str | None,
    entry: str | None, metadata: dict[str, str], work_task_id: str | None,
) -> None:
    """同一请求不能改包、入口、本人或服务端任务关联。"""
    if (record.user_id, record.package, record.version, record.entry, record.metadata,
        record.work_task_id) != (principal.user_id, package, version, entry, metadata, work_task_id):
        raise ConflictError("请求标识已用于不同的业务会话")


def require_task_executor(record: SessionRecord, work_task_id: str | None) -> None:
    """普通HTTP会话写入口不接受任务会话，任务只能经托管执行器推进。"""
    if record.work_task_id != work_task_id:
        raise ConflictError("任务会话请通过工作任务入口操作")


def scope_for(record: SessionRecord) -> ExecutionScope:
    """从持久服务端字段构造执行空间，不解释客户端metadata。"""
    execution = ("task:" + record.work_task_id) if record.work_task_id else ("session:" + record.session_id)
    return ExecutionScope(record.tenant_id, record.user_id or "service:" + record.created_by, execution)
