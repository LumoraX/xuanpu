"""会诊协调（R5-D1）：一步会诊全部完成后比对、选路，高分歧交员工裁决。

- 成员各自只拿到自己的任务说明，看不到彼此的过程；比对只用各成员已发布的结论（摘要）。
- 比对：确定性敏感话题识别 + 模型评级（``DivergenceRater``），模型只决定走哪条路。
- 高分歧：建一个「裁决」待处理项并在主对话里并排列出各成员结论。待处理项按
  ``ruling:<编组>:<步>`` 去重、消息按 ``butler:group:<编组>:ruling:<步>`` 去重，
  进程在任何两步之间被终止，重做都不会多出第二个裁决项或第二条消息。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from xuanpu_server.butler.groups.divergence import (
    MemberConclusion,
    choose_path,
    sensitive_topics,
    valid_rating,
)
from xuanpu_server.butler.groups.verdict import StepVerdict, ruling_question
from xuanpu_server.butler.wake_models import PendingItem
from xuanpu_server.errors import PlatformError
from xuanpu_server.ids import new_id
from xuanpu_server.tenancy import Principal

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.butler.groups.divergence import DivergenceRater
    from xuanpu_server.butler.groups.plan import GroupRecord
    from xuanpu_server.platform import Platform
    from xuanpu_server.tracing.models import TraceOwner

logger = logging.getLogger(__name__)


def ruling_key(group_id: str, step: int) -> str:
    """裁决待处理项的去重键。"""
    return f"ruling:{group_id}:{step}"


def parse_ruling_key(key: str) -> tuple[str, int]:
    """去重键 → (编组, 步)；格式不对报值错误。"""
    prefix, group_id, step = key.split(":")
    if prefix != "ruling":
        raise ValueError("不是裁决项")
    return group_id, int(step)


def question_of(record: GroupRecord, step: int) -> str:
    """这一步会诊要回答的问题：计划标题加各成员的任务说明。"""
    briefs = "\n".join(f"- {m.brief}" for m in record.plan.steps[step].members)
    return f"{record.plan.title}\n{briefs}"


def conclusions_of(record: GroupRecord, step: int) -> tuple[MemberConclusion, ...]:
    """这一步完成了的成员发布的结论，按成员序号。"""
    found = []
    for index, member in enumerate(record.plan.steps[step].members):
        run = record.run(step, index)
        if run is not None and run.outcome == "completed":
            found.append(MemberConclusion(number=index + 1, artisan=member.package,
                                          text=run.summary or "（这位匠人没有留下文字结论）"))
    return tuple(found)


class ConsultCoordinator:
    """比对与裁决项。"""

    def __init__(self, platform: Platform, rater: DivergenceRater | None, *,
                 now: Callable[[], float]) -> None:
        """``rater`` 为 None 时一律按「评不出」走高分歧（交员工）。"""
        self._platform = platform
        self._rater = rater
        self._now = now

    async def judge(self, owner: TraceOwner, record: GroupRecord) -> StepVerdict:
        """比对当前步并选路；模型失败不抛出，按评不出处理。"""
        step = record.step
        conclusions = conclusions_of(record, step)
        question = question_of(record, step)
        rating = None
        if self._rater is not None:
            try:
                rating = await self._rater.rate(owner, record.trace_id, question, conclusions)
            except PlatformError as exc:
                logger.info("会诊比对不可用（%s），交员工裁决", exc.code)
        rating = valid_rating(rating, len(record.plan.steps[step].members))
        sensitive = sensitive_topics([question, *(c.text for c in conclusions)])
        level, reasons = choose_path(rating, sensitive)
        return StepVerdict(
            step=step, level=level, reasons=reasons,
            rated_level=None if rating is None else rating.level,
            synthesis="" if rating is None else rating.synthesis,
            differences=() if rating is None else rating.differences,
            basis=() if rating is None else rating.basis)

    async def open_ruling(self, owner: TraceOwner, record: GroupRecord,
                          verdict: StepVerdict) -> StepVerdict:
        """建裁决项并在主对话里并排给出结论；返回带裁决项标识的裁决。"""
        step = record.step
        anchor = next(r for r in record.runs if r.step == step and r.dispatch_id is not None)
        assert anchor.dispatch_id is not None
        dispatch = await self._platform.dispatches.get(owner, anchor.dispatch_id)
        assert dispatch.host_ref is not None
        question = ruling_question(verdict, conclusions_of(record, step))
        now = datetime.fromtimestamp(self._now(), UTC)
        item, _ = await self._platform.store.wake_store.open_pending(PendingItem(
            pending_id=new_id("pnd"), tenant_id=owner.tenant_id, user_id=owner.user_id,
            dispatch_id=dispatch.dispatch_id, source_key=ruling_key(record.group_id, step),
            kind="ruling", question=question, host=dispatch.host, host_ref=dispatch.host_ref,
            created_at=now, updated_at=now))
        who = Principal(role="tenant_member", tenant_id=owner.tenant_id, user_id=owner.user_id,
                        key_id=None)
        await self._platform.workspace.append_message(
            who, "assistant", question, f"butler:group:{record.group_id}:ruling:{step}",
            metadata={"butler": {"trace_id": record.trace_id, "group_id": record.group_id,
                                 "event": "group_ruling", "pending_id": item.pending_id}})
        return verdict.model_copy(update={"pending_id": item.pending_id})
