"""仅根轮结束能推进办公结算；子skill完成与文字回复不证明成果存在。"""

from __future__ import annotations

from typing import Any

from xuanpu_server.errors import RuntimeCleanupRequiredError
from xuanpu_server.runtime.operation_status import operation_outcome


def stage_outcome(kind: str, data: dict[str, Any]) -> str | None:
    """复用R0结果分类，保留部分批准和未知结果的区别。"""
    if kind in {'turn_completed', 'turn_failed'} and not data.get('is_root', False):
        return None
    if kind not in {'turn_completed', 'turn_failed', 'turn_suspended',
                    'suspension_resolve_rejected', 'suspension_partially_resolved', 'suspension_resolved'}:
        return None
    if kind == 'suspension_resolved' and not data.get('aborted', False):
        return None
    outcome = operation_outcome(kind, data)
    return {'suspended': 'waiting_user', 'resume_partial': 'waiting_user',
            'resume_rejected': 'waiting_user', 'aborted': 'failed', 'completed': 'verify',
            'failed': 'failed', 'cancelled': 'cancelled', 'outcome_unknown': 'outcome_unknown'}[outcome]


class TaskExecutionError(Exception):
    """可向本人展示的稳定办公失败，不包含供应商响应或内部栈。"""

    def __init__(self, code: str, message: str) -> None:
        """代码与中文说明由宿主分支固定，不从不可信脚本错误原文复制。"""
        super().__init__(message)
        self.code, self.message = code, message


def cleanup_uncertain(error: BaseException) -> bool:
    """装配尚未移交的资源也属于持久清理责任，包含聚合异常。"""
    if isinstance(error, RuntimeCleanupRequiredError):
        return True
    return isinstance(error, BaseExceptionGroup) and any(cleanup_uncertain(child) for child in error.exceptions)
