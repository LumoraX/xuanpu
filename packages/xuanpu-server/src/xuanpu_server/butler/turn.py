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
    from xuanpu_server.butler.groups.divergence import Rating
    from xuanpu_server.butler.models import DispatchRecord
    from xuanpu_server.butler.wake_models import AnswerSource
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.tracing.models import TraceOwner
    from xuanpu_server.work_tasks.models import InputFile


@dataclass
class WakeContext:
    """被唤醒的一轮：哪次派工、匠人这一轮说了什么、能否代答、代答可用的出处原文。

    Attributes:
        dispatch: 被唤醒的派工。
        artisan_text: 匠人这一轮的回复（数据）。
        can_answer: 宿主能接收答复（会话承载且这一轮正常结束）。
        corpus: 代答出处原文，按出处类别。
        answer: 管家拟代答的内容与出处（经 ``answer_artisan`` 登记并已初核）。
        question: 管家拟转问员工的问题（经 ``ask_user`` 登记）。
        proxy_rejected: 管家试过代答但不合规（匠人在提问，又不能代答，只能转问）。
    """

    dispatch: DispatchRecord
    artisan_text: str
    can_answer: bool
    corpus: dict[str, str] = field(default_factory=dict)
    answer: tuple[str, AnswerSource] | None = None
    question: str | None = None
    proxy_rejected: bool = False


@dataclass
class CompareContext:
    """会诊比对的一轮（R5-D1）：参与比对的成员数，以及经 ``rate_divergence`` 登记的评级。"""

    members: int
    rating: Rating | None = None


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
        user_text: 员工这句话的原文（答复待处理项时原样转给匠人，不用模型改写的文本）。
        wake: 被唤醒的一轮的上下文；员工输入触发的一轮为 None。
        answered: 本轮答复了的待处理项。
        groups: 本轮建立或回放的编组标识（R5-D）。
        compare: 会诊比对的一轮的上下文；其余轮次为 None。
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
    user_text: str = ""
    wake: WakeContext | None = None
    answered: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    compare: CompareContext | None = None
    deadline: asyncio.Timeout | None = None
    """本轮可改期的时限（R6-B）；``extend_turn`` 经它延长。"""
    started_at: float = 0.0
    """本轮开始时的事件循环时间；延长不超过它加 ``max_turn_seconds``。"""

    def has_output(self) -> bool:
        """本轮是否产出了回复之外的结果（草稿、派工、答复、代答或转问）。"""
        wake = self.wake
        return (self.proposal is not None or bool(self.dispatches) or bool(self.answered)
                or bool(self.groups)
                or (self.compare is not None and self.compare.rating is not None)
                or (wake is not None and (wake.answer is not None or wake.question is not None)))

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
