"""统一运行终态，不把停止请求或失败完成视为业务成功。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


def operation_outcome(kind: str, data: Mapping[str, Any]) -> str:
    """根据确定事件分类；外部结果未知需要另行核实。"""
    if kind == "turn_suspended":
        unknown = data.get("reason") == "tool_outcome_unknown" or any(
            isinstance(item, dict) and item.get("reason") == "tool_outcome_unknown"
            for item in data.get("pending", [])
        )
        return "outcome_unknown" if unknown else "suspended"
    if kind == "suspension_resolved" and data.get("aborted") is True:
        return "aborted"
    if kind == "suspension_resolve_rejected":
        return "resume_rejected"
    if kind == "suspension_partially_resolved":
        return "resume_partial"
    if data.get("end_reason") == "cancelled" or data.get("kind") == "cancelled":
        return "cancelled"
    if kind == "turn_completed":
        return "failed" if data.get("success") is False else "completed"
    return "failed"
