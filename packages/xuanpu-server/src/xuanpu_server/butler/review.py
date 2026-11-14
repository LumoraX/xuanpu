"""被唤醒的一轮管家（R5-C2）：复核匠人结果，必要时代答或转问。

- 管家只拿到数据：原请求、任务说明、匠人这一轮的回复、确定性检查结果。
- 复核文本只作「辅助判断」，不改变确定性结论：没通过就是没通过。
- 代答由 ``answer_artisan`` 登记，平台按出处规则再核一次；不合规的一律改为转问员工。
- 预算超限、模型不可用或这一轮失败时不复核，只按确定性结论交付（并如实说明）。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from xuanpu_server.butler.budget import ButlerBudgetError, TraceUsage, check_butler_turn
from xuanpu_server.butler.proxy import check_proxy
from xuanpu_server.butler.turn import ButlerTurn, WakeContext
from xuanpu_server.errors import NotFoundError, PlatformError
from xuanpu_server.tracing.models import TERMINAL_STATES

if TYPE_CHECKING:
    from xuanpu_server.butler.engine import ButlerEngines
    from xuanpu_server.butler.models import DispatchRecord
    from xuanpu_server.butler.wake_models import ButlerEvent, WakeDecision
    from xuanpu_server.platform import Platform
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.tracing.models import TraceOwner

logger = logging.getLogger(__name__)
REVIEW_MAX = 1500


class Reviewer:
    """用管家引擎跑被唤醒的一轮。"""

    def __init__(self, platform: Platform, engines: ButlerEngines) -> None:
        """依赖由平台注入。"""
        self._platform = platform
        self._engines = engines

    async def review(self, who: Principal, event: ButlerEvent, dispatch: DispatchRecord,
                     base: WakeDecision, *, can_answer: bool) -> WakeDecision:
        """在确定性结论上加复核；能代答或需转问时改写动作，不改写验收结论。"""
        owner = TraceOwnerOf.of(dispatch)
        try:
            check_butler_turn(self._platform.butler_config, await self._usage(owner, dispatch))
        except ButlerBudgetError as exc:
            logger.info("管家复核跳过（%s）", exc.reason)
            return base
        corpus = await self.corpus(who, dispatch)
        wake = WakeContext(dispatch=dispatch, artisan_text=base.artisan_text or "",
                           can_answer=can_answer, corpus=corpus)
        settings = await self._platform.workspace.settings(who)
        turn = ButlerTurn(who=who, request_id=f"wake-{event.event_id}", trace_id=dispatch.trace_id,
                          timezone=str(settings["timezone"]), wake=wake)
        try:
            reply = await self._engines.run(turn, self._payload(dispatch, base, wake, corpus))
        except PlatformError as exc:
            logger.info("管家复核不可用（%s）", exc.code)
            return base
        return self._decide(base, wake, reply)

    def _decide(self, base: WakeDecision, wake: WakeContext, reply: str) -> WakeDecision:
        """把这一轮的登记折成决定；代答再核一次出处。"""
        review = reply.strip()[:REVIEW_MAX] or None
        if wake.answer is not None and wake.can_answer:
            answer, source = wake.answer
            verdict = check_proxy(wake.artisan_text, answer, source, wake.corpus)
            if verdict.allowed:
                return base.model_copy(update={"action": "forward", "answer": answer,
                                               "source": source, "review": review})
            return base.model_copy(update={"action": "ask_user", "review": review,
                                           "question": wake.artisan_text[:2000] or None})
        if wake.question is not None and wake.can_answer:
            return base.model_copy(update={"action": "ask_user", "question": wake.question,
                                           "review": review})
        if wake.proxy_rejected and wake.can_answer:
            # 匠人在提问、管家又不能代答：原样转问员工，不能当作结果交付
            return base.model_copy(update={"action": "ask_user", "review": review,
                                           "question": wake.artisan_text[:2000] or None})
        return base.model_copy(update={"review": review})

    async def corpus(self, who: Principal, dispatch: DispatchRecord) -> dict[str, str]:
        """代答可用的出处原文：本人设置、本次请求原话与本人此前对这次派工的答复。"""
        p = self._platform
        settings = await p.workspace.settings(who)
        user = await p.identity.store.get_user(dispatch.tenant_id, dispatch.user_id)
        setting = "\n".join(str(settings.get(k, "")) for k in ("user_title", "assistant_name",
                                                                "style", "timezone"))
        if user is not None:
            setting += "\n" + user.display_name
        base = dispatch.request_id.rsplit(":d", 1)[0]
        original = await p.workspace.find_message(who, base + ":user")
        said = [str(original["text"])] if original is not None else []
        owner = TraceOwnerOf.of(dispatch)
        for item in await p.store.wake_store.list_pending(owner, open_only=False,
                                                          dispatch_id=dispatch.dispatch_id):
            if item.answer_source is not None and item.answer_source.kind == "employee":
                said.append(item.answer or "")
        return {"setting": setting, "conversation": "\n".join(said)}

    async def _usage(self, owner: TraceOwner, dispatch: DispatchRecord) -> TraceUsage:
        """这条 trace 已跑的管家轮数与已结束执行的 token。"""
        try:
            view = await self._platform.store.trace_store.get(owner, dispatch.trace_id)
        except NotFoundError:
            return TraceUsage()
        turns = sum(1 for s in view.spans if s.kind == "butler" and s.parent_span_id is None)
        tokens: int | None = 0
        for span in view.spans:
            if span.state not in TERMINAL_STATES or not span.attempts:
                continue
            if span.input_tokens is None or span.output_tokens is None:
                tokens = None
                break
            tokens = (tokens or 0) + span.input_tokens + span.output_tokens
        return TraceUsage(butler_turns=turns, tokens=tokens)

    @staticmethod
    def _payload(dispatch: DispatchRecord, base: WakeDecision, wake: WakeContext,
                 corpus: dict[str, str]) -> str:
        """被唤醒的一轮的输入：全部是数据。"""
        return json.dumps({
            "purpose": "artisan_round_finished", "artisan": dispatch.artisan.package,
            "instruction": dispatch.instruction, "artisan_reply": wake.artisan_text[:4000],
            "checks": base.checks, "passed": base.passed, "can_answer": wake.can_answer,
            "employee_said": corpus.get("conversation", "")[:4000],
        }, ensure_ascii=False)


class TraceOwnerOf:
    """派工 → 归属。"""

    @staticmethod
    def of(dispatch: DispatchRecord) -> TraceOwner:
        """派工的主人。"""
        from xuanpu_server.tracing.models import TraceOwner

        return TraceOwner(tenant_id=dispatch.tenant_id, user_id=dispatch.user_id)
