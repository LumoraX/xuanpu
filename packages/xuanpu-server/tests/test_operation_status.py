"""运行时事件与业务结果不能混为一谈。"""
from __future__ import annotations

import pytest
from xuanpu_server.runtime.operation_status import operation_outcome


@pytest.mark.parametrize(("kind", "data", "expected"), [
    ("turn_completed", {"success": False}, "failed"),
    ("turn_completed", {"success": True}, "completed"),
    ("turn_completed", {"end_reason": "cancelled", "success": False}, "cancelled"),
    ("turn_failed", {"kind": "cancelled"}, "cancelled"),
    ("turn_suspended", {"reason": "tool_outcome_unknown"}, "outcome_unknown"),
    ("turn_suspended", {"reason": "permission"}, "suspended"),
])
def test_terminal_outcome(kind: str, data: dict, expected: str) -> None:
    """失败完成事件不能记成成功，未知结果不能记成未执行。"""
    assert operation_outcome(kind, data) == expected


def test_system_abort_is_not_success() -> None:
    """内核已明确不再续跑的系统中止不是业务成功。"""
    assert operation_outcome("suspension_resolved", {"aborted": True}) == "aborted"
