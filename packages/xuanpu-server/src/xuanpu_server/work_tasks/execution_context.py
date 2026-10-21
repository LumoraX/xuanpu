"""执行所有权只在平台内部传递，覆盖内核子任务和工具调用。"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.work_tasks.execution_models import AttemptLease
    from xuanpu_server.work_tasks.execution_store import ExecutionStore
    from xuanpu_server.workspace.models import WorkspaceState


@dataclass
class ExecutionBinding:
    """某次attempt的不可跨任务复用的执行上下文。"""

    lease: AttemptLease
    store: ExecutionStore
    now: Callable[[], float]
    stop_reason: str | None = None
    max_task_tokens: int = 150000


    def at(self, state: WorkspaceState) -> float:
        """PG提交以数据库时钟为准，显式测试时钟按注入值。"""
        return self.store.transaction_time(state, self.now())


current_execution: ContextVar[ExecutionBinding | None] = ContextVar('task_execution', default=None)
