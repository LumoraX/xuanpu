"""管家一轮处理的上下文：谁、哪条 trace、本轮允许用哪些材料、产出了什么。

工具处理函数只拿得到内核的线程标识；平台按线程标识找回本轮上下文，身份与权限都从这里取，
不从模型参数取。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from xuanpu_server.errors import PermissionDeniedError

if TYPE_CHECKING:
    from xuanpu_server.assistant_reminders import RouteOutcome
    from xuanpu_server.butler.models import DispatchRecord
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.tracing.models import TraceOwner
    from xuanpu_server.work_tasks.models import InputFile


@dataclass
class ButlerTurn:
    """一轮管家处理。

    Attributes:
        who: 已认证的员工。
        request_id: 员工这条输入的幂等标识。
        trace_id: 本次请求的 trace（派工记录挂在它上面）。
        timezone: 员工的时区。
        materials: 员工本次明确提供的文件版本；派工只能从中选。
        proposal: 本轮提出的待确认草稿（每轮最多一个）。
        dispatches: 本轮建立或回放的派工。
        root_span: 管家根执行的 span 标识（``ex.<execution_id>``），见到根执行开始后才有。
    """

    who: Principal
    request_id: str
    trace_id: str
    timezone: str
    materials: tuple[InputFile, ...] = ()
    proposal: RouteOutcome | None = None
    dispatches: list[DispatchRecord] = field(default_factory=list)
    root_span: str | None = None
    root_seen: asyncio.Event = field(default_factory=asyncio.Event)

    def note_root(self, span_id: str) -> None:
        """记下根执行 span；只记第一次。"""
        if self.root_span is None:
            self.root_span = span_id
            self.root_seen.set()

    def owner(self) -> TraceOwner:
        """本轮归属；管家只为员工工作，没有员工身份是装配错误。"""
        from xuanpu_server.tracing.models import TraceOwner

        if self.who.tenant_id is None or self.who.user_id is None:
            raise PermissionDeniedError("管家需要员工身份")
        return TraceOwner(tenant_id=self.who.tenant_id, user_id=self.who.user_id)

    def dispatch_request_id(self) -> str:
        """本轮下一个派工的幂等标识：同一输入重试时按相同顺序得到相同标识，不会重复派工。"""
        return f"{self.request_id}:d{len(self.dispatches) + 1}"


class TurnRegistry:
    """线程标识 → 本轮上下文。只在本进程内、只在一轮处理期间有效。"""

    def __init__(self) -> None:
        """空登记表。"""
        self._turns: dict[str, ButlerTurn] = {}

    def bind(self, thread_id: str, turn: ButlerTurn) -> None:
        """登记；同一线程不能同时属于两轮。"""
        if thread_id in self._turns:
            raise RuntimeError("管家线程已在处理另一轮")
        self._turns[thread_id] = turn

    def unbind(self, thread_id: str) -> None:
        """本轮结束后注销。"""
        self._turns.pop(thread_id, None)

    def get(self, thread_id: str) -> ButlerTurn | None:
        """按线程找本轮上下文；找不到返回 None，由调用方明确报错。"""
        return self._turns.get(thread_id)
