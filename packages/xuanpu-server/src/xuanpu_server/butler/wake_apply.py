"""按唤醒决定执行副作用（R5-C2）。每一步都可安全重做：

- 给员工的消息按 ``butler:<事件>:<用途>`` 写进主对话（同一请求标识只写一次）。
- 待处理项按 ``(派工, source_key)`` 去重；代答转发按编号查重。
- 派工状态只在允许的方向上推进，已到的状态不再推。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from xuanpu_server.butler.models import FINISHED_STATES
from xuanpu_server.butler.pending import owner_of
from xuanpu_server.butler.wake_models import PendingItem
from xuanpu_server.ids import new_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.butler.models import DispatchRecord, DispatchState
    from xuanpu_server.butler.pending import PendingService
    from xuanpu_server.butler.wake_models import ButlerEvent, WakeDecision
    from xuanpu_server.platform import Platform
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.tracing.models import TraceOwner

CHECK_LABELS = {
    "terminal": "匠人还没有真正结束", "succeeded": "匠人报告失败或被停止",
    "outputs": "声明的成果缺失或读不出", "no_pending": "还有待确认的操作",
    "no_unknown": "有操作的外部结果未知，需要人工核实",
}
REVIEW_LABEL = "辅助判断"
EXCERPT = 1500


class WakeEffects:
    """唤醒决定的执行者。"""

    def __init__(self, platform: Platform, pending: PendingService, *,
                 now: Callable[[], float]) -> None:
        """依赖由平台注入。"""
        self._platform = platform
        self._pending = pending
        self._now = now

    def _at(self) -> datetime:
        """当前时间（UTC）。"""
        return datetime.fromtimestamp(self._now(), UTC)

    @staticmethod
    def owner(who: Principal) -> TraceOwner:
        """员工身份 → 归属。"""
        return owner_of(who)

    async def apply(self, who: Principal, event: ButlerEvent, dispatch: DispatchRecord,
                    decision: WakeDecision) -> None:
        """按决定执行。"""
        owner = owner_of(who)
        if decision.action != "await_approval":
            # 宿主已经继续或结束：此前等审批的项随之解决
            await self._pending.resolve_for_dispatch(owner, dispatch.dispatch_id, kinds=("approval",))
        if decision.action == "deliver":
            await self._deliver(who, event, dispatch, decision)
        elif decision.action == "forward":
            await self._forward(who, event, dispatch, decision)
        elif decision.action == "ask_user":
            await self._ask(who, event, dispatch, decision)
        else:
            await self._await_approval(who, event, dispatch, decision)

    async def _deliver(self, who: Principal, event: ButlerEvent, dispatch: DispatchRecord,
                       decision: WakeDecision) -> None:
        """验收结论 → 派工终态 → 交付消息 → delivered。"""
        owner = owner_of(who)
        await self._pending.resolve_for_dispatch(owner, dispatch.dispatch_id, kinds=("question",))
        final: DispatchState = decision.final_state or "failed"
        await self._move(owner, dispatch.dispatch_id, final, end_reason=decision.end_reason)
        await self._message(who, event, "deliver", _delivery_text(dispatch, decision), dispatch,
                            {"passed": decision.passed, "checks": decision.checks,
                             "review_label": REVIEW_LABEL if decision.review else None})
        await self._move(owner, dispatch.dispatch_id, "delivered")

    async def _forward(self, who: Principal, event: ButlerEvent, dispatch: DispatchRecord,
                       decision: WakeDecision) -> None:
        """代答：登记带出处的代答记录，转给匠人，并告诉员工答了什么、依据什么。"""
        assert decision.answer is not None and decision.source is not None
        item = await self._open_item(dispatch, f"proxy:{event.event_id}", "proxied",
                                     decision.artisan_text or "匠人提问")
        if item.state != "answered":
            await self._pending.forward(who, item, decision.answer, decision.source,
                                        request_id=f"butler:{event.event_id}:proxy")
        text = (f"「{_name(dispatch)}」问：{_excerpt(decision.artisan_text)}\n"
                f"我已按你说过的原话代为答复：{decision.answer}（出处：{_source(decision)}）。")
        await self._message(who, event, "proxy", text, dispatch,
                            {"source": decision.source.model_dump()})

    async def _ask(self, who: Principal, event: ButlerEvent, dispatch: DispatchRecord,
                   decision: WakeDecision) -> None:
        """转问员工：建问题项，派工进入 asking，在对话里提问。"""
        question = decision.question or _excerpt(decision.artisan_text) or "匠人需要你补充信息。"
        item = await self._open_item(dispatch, f"ask:{event.event_id}", "question", question)
        await self._move(owner_of(who), dispatch.dispatch_id, "asking")
        await self._message(who, event, "ask", f"「{_name(dispatch)}」需要你回答：{question}",
                            dispatch, {"pending_id": item.pending_id})

    async def _await_approval(self, who: Principal, event: ButlerEvent, dispatch: DispatchRecord,
                              decision: WakeDecision) -> None:
        """宿主等本人审批：建审批项（不代答），派工进入 asking，提示员工去待确认处理。"""
        question = f"「{_name(dispatch)}」有一项操作需要你确认，请在待确认里处理。"
        item = await self._open_item(dispatch, f"approval:{event.key}", "approval", question)
        await self._move(owner_of(who), dispatch.dispatch_id, "asking")
        await self._message(who, event, "approval", question, dispatch,
                            {"pending_id": item.pending_id})

    async def _open_item(self, dispatch: DispatchRecord, key: str, kind: str, question: str,
                         ) -> PendingItem:
        """按键幂等地建待处理项。"""
        assert dispatch.host_ref is not None
        now = self._at()
        item, _ = await self._platform.store.wake_store.open_pending(PendingItem(
            pending_id=new_id("pnd"), tenant_id=dispatch.tenant_id, user_id=dispatch.user_id,
            dispatch_id=dispatch.dispatch_id, source_key=key, kind=kind,  # type: ignore[arg-type]
            question=question[:2000], host=dispatch.host, host_ref=dispatch.host_ref,
            created_at=now, updated_at=now))
        return item

    async def _move(self, owner: TraceOwner, dispatch_id: str, to: DispatchState, *,
                    end_reason: str | None = None) -> None:
        """派工推进到目标状态；已在该状态或已越过时不动。"""
        store = self._platform.dispatches
        current = await store.get(owner, dispatch_id)
        if current.state == to or current.state == "delivered":
            return
        if to == "asking" and current.state != "running":
            return
        if to in FINISHED_STATES and current.state in FINISHED_STATES:
            return
        await store.update(owner, dispatch_id, expected_version=current.version, to=to,
                           at=self._at(), end_reason=end_reason)

    async def _message(self, who: Principal, event: ButlerEvent, purpose: str, text: str,
                       dispatch: DispatchRecord, extra: dict[str, Any]) -> None:
        """写进员工主对话；同一事件同一用途只写一次。"""
        await self._platform.workspace.append_message(
            who, "assistant", text, f"butler:{event.event_id}:{purpose}",
            metadata={"butler": {"trace_id": dispatch.trace_id, "dispatch_id": dispatch.dispatch_id,
                                 "event": purpose, **extra}})


def _name(dispatch: DispatchRecord) -> str:
    """给员工看的匠人名。"""
    return dispatch.artisan.package


def _excerpt(text: str | None) -> str:
    """匠人回复摘录。"""
    if not text:
        return ""
    return text if len(text) <= EXCERPT else text[:EXCERPT] + "…"


def _source(decision: WakeDecision) -> str:
    """出处说明。"""
    assert decision.source is not None
    label = {"setting": "你的设置", "conversation": "你本次说的话", "employee": "你的答复"}
    return f"{label[decision.source.kind]}「{decision.source.quote}」"


def _delivery_text(dispatch: DispatchRecord, decision: WakeDecision) -> str:
    """交付消息：确定性结论在前，模型复核单列并标「辅助判断」。"""
    if decision.passed:
        head = f"「{_name(dispatch)}」已完成，结果如下：\n{_excerpt(decision.artisan_text)}"
    elif decision.final_state == "cancelled":
        head = f"「{_name(dispatch)}」已停止，没有完成这项工作。"
    else:
        failed = [CHECK_LABELS[c] for c, ok in decision.checks.items() if not ok]
        head = (f"「{_name(dispatch)}」的结果没有通过验收：{'；'.join(failed)}。"
                "可以重试、换一位匠人或补充材料后再交给我；我不会自动重跑。")
        if decision.artisan_text:
            head += f"\n匠人的原话：{_excerpt(decision.artisan_text)}"
    if decision.review:
        head += f"\n\n{REVIEW_LABEL}（仅供参考，不改变上面的结论）：{decision.review}"
    return head
