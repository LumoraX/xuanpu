"""跨引擎的 trace 绑定：一个引擎的根执行挂到别处（例如管家派工）已有的 trace 与父 span 下。

绑定只来自持久记录（派工记录与其承载的任务或会话），由平台按会话标识查出，不按时间或
内容猜父级。查不到绑定的工作照旧单独成 trace。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from xuanpu_server.tracing.models import TraceOwner


@dataclass(frozen=True)
class TraceBinding:
    """根执行应写入的 trace 与它的父 span。"""

    trace_id: str
    parent_span_id: str


class TraceBinder(Protocol):
    """按会话查绑定。"""

    async def bind(self, owner: TraceOwner, session_id: str) -> TraceBinding | None:
        """这个会话的根执行应挂到哪里；没有绑定返回 None。查询失败直接抛出。"""
        ...
