"""审批认领与重复请求的不可变回执。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from xuanpu_server.errors import ApprovalConflictError, InvalidRequestError


@dataclass(frozen=True)
class OperationReceipt:
    """一次决定的认领状态；applied 仅表示审批核销，不表示工具写入成功。"""

    operation_id: str
    fingerprint: str
    state: str = "claimed"
    submission_id: str | None = None
    replay: bool = False


def decision_fingerprint(decisions: dict[str, Any]) -> str:
    """严格编码整个决定集合，不存储表单原文。"""
    if (not decisions or len(decisions) > 256 or any(
        not isinstance(key, str) or not key.strip() or len(key) > 256 for key in decisions
    )):
        raise InvalidRequestError("审批决定必须包含有效请求标识")
    try:
        value = json.dumps(decisions, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError("审批决定不是有效JSON") from exc
    encoded = value.encode()
    if len(encoded) > 1024 * 1024:
        raise InvalidRequestError("审批决定超过 1 MiB 上限")
    return hashlib.sha256(encoded).hexdigest()


def replay_receipt(rows: list[OperationReceipt], fingerprint: str, count: int) -> OperationReceipt:
    """只有整组相同且已绑定提交的决定可以回放，其他情况一律不再次执行。"""
    first = rows[0]
    if any(row.state == "outcome_unknown" for row in rows):
        unknown = next(row for row in rows if row.state == "outcome_unknown")
        raise ApprovalConflictError("原审批结果待核实，不能重复执行",
            operation_id=unknown.operation_id, state=unknown.state, submission_id=unknown.submission_id)
    if len(rows) != count or any(row.fingerprint != fingerprint for row in rows):
        raise ApprovalConflictError("审批请求已经被其他决定认领", operation_id=first.operation_id, state=first.state,
            submission_id=first.submission_id)
    if any(row.operation_id != first.operation_id for row in rows) or not first.submission_id:
        raise ApprovalConflictError("原审批仍在处理中或需核实，请查询原请求", operation_id=first.operation_id, state=first.state,
            submission_id=first.submission_id)
    return OperationReceipt(first.operation_id, fingerprint, first.state, first.submission_id, True)
