"""工作任务的请求指纹、状态机与终态约束。"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import ConflictError
from xuanpu_server.work_tasks.models import TERMINAL, WorkTaskRecord

if TYPE_CHECKING:
    from xuanpu_server.work_tasks.models import WorkTaskInput

ALLOWED = {
    "accepted": {"running", "cancelled", "failed", "interrupted", "outcome_unknown"},
    "running": {"running", "waiting_user", *TERMINAL},
    "waiting_user": {"waiting_user", "running", "succeeded", "cancelled", "failed", "interrupted", "outcome_unknown"},
}


def input_fingerprint(request: WorkTaskInput) -> str:
    """同一请求内容采用标准JSON摘要，排除传输用的请求标识。"""
    raw = json.dumps(request.model_dump(mode="json", exclude={"request_id"}), sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def advance(
    record: WorkTaskRecord, status: str, *, expected_version: int, at: float, **changes: Any
) -> WorkTaskRecord:
    """以期望版本推进任务，终态不回退，成功要求成果和决定回执。"""
    if record.state_version != expected_version or status not in ALLOWED.get(record.status, set()):
        raise ConflictError("任务状态已变化，请重新读取")
    if at < record.updated_at:
        raise ConflictError("任务更新时间不能回退")
    protected = {"task_id", "tenant_id", "user_id", "actor", "request_id", "input_fingerprint",
                 "inputs", "package", "package_version", "package_digest", "contains_external_input",
                 "accepted_at", "state_version", "status", "finished_at", "updated_at",
                 "text", "timezone", "matter_id", "output_kind", "definition_id", "definition_version", "trigger_origin_chain", "grant_id", "execution_kind", "entry",
                 "completion_policy", "recovery_policy_version"}
    if protected.intersection(changes):
        raise ConflictError("任务身份、输入与能力版本不可修改")
    updated = WorkTaskRecord.model_validate({**record.model_dump(), **changes, "status": status,
        "state_version": expected_version + 1, "updated_at": at,
        "finished_at": at if status in TERMINAL else None})
    if status == "succeeded" and (
        (updated.completion_policy == 'office_files' and not updated.artifacts)
        or (updated.completion_policy == 'capability_receipt' and (
            updated.result_receipt.get('verified') is not True
            or not updated.result_receipt.get('submission_id')
            or updated.result_receipt.get('session_id') != updated.session_id))
        or updated.unverified_write or not updated.outputs_verified or not updated.decisions_complete
        or updated.cancel_requested
    ):
        raise ConflictError("成果尚未保存核验、本人决定未完成或已请求取消")
    return updated
