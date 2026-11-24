"""会诊消息区的推进（R5-D2）：发布成员结论、按 @ 派出定向追问、超过轮数由管家收口。

每次推进只做幂等的事，最多派出一次追问（由调用方保存后再推进）：
1. 把已完成成员的结论与已完成追问的回复发布到消息区（按 ``pub:<派工>`` 去重）。
2. 还有追问未出结果：等待。
3. 消息里 @ 到同一步其他成员、且下一轮不超过上限：为第一个尚未派出的追问派工
   （幂等标识 ``grp:<编组>:<步>:<成员>:f<提问消息序号>``），返回带新追问的记录。
4. 有 @ 因超过轮数没被派出：管家追加一条收口说明（按 ``close:<步>`` 去重）。
消息内容是数据：@ 只能触发对同一步既有成员的一次追问，不能改计划、加成员或改权限。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from server.engine.butler.groups.mentions import followup_brief, parse_mentions
from server.engine.butler.groups.messages import GroupMessage
from server.engine.butler.groups.plan import FollowUp, with_followup

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from server.engine.butler.groups.messages import MessageStore
    from server.engine.butler.groups.plan import GroupRecord, MemberOutcome
    from server.engine.tracing.models import TraceOwner

    # (编组, 成员下标, 任务说明, 幂等标识) → (派工, 立即失败的结果, 结束原因)
    Starter = Callable[[GroupRecord, int, str, str],
                       Awaitable[tuple[str | None, MemberOutcome | None, str | None]]]

BoardState = Literal["wait", "settled"]
NO_TEXT = "（这位匠人没有留下文字结论）"


class ConsultBoard:
    """一步会诊的消息区推进。"""

    def __init__(self, messages: MessageStore, start: Starter, *,
                 max_rounds: Callable[[], int], now: Callable[[], float]) -> None:
        """``start`` 用编组执行器的派工入口；轮数上限每次现取配置。"""
        self._messages = messages
        self._start = start
        self._max_rounds = max_rounds
        self._now = now

    async def tend(self, owner: TraceOwner, record: GroupRecord) -> GroupRecord | BoardState:
        """推进一次：返回带新追问的记录（须保存）、``wait`` 或 ``settled``。"""
        await self._publish(owner, record)
        step = record.step
        if any(f.step == step and f.outcome is None for f in record.followups):
            return "wait"
        board = await self._messages.list(owner, record.group_id)
        over_limit = False
        for message in (m for m in board if m.step == step and m.author == "member"):
            for target in message.mentions:
                if record.followup(message.seq, target) is not None:
                    continue
                if message.round + 1 > self._max_rounds():
                    over_limit = True
                    continue
                return with_followup(record, await self._ask(record, board, message, target))
        if over_limit:
            await self._append(owner, record, key=f"close:{step}", author="butler", member=None,
                               round_=self._max_rounds(),
                               text=f"追问已达上限（{self._max_rounds()} 轮），由管家收口比对。")
        return "settled"

    async def _publish(self, owner: TraceOwner, record: GroupRecord) -> None:
        """发布已完成成员的结论与已完成追问的回复。"""
        step = record.step
        for run in record.runs:
            if run.step == step and run.outcome == "completed" and run.dispatch_id is not None:
                await self._append(owner, record, key=f"pub:{run.dispatch_id}", author="member",
                                   member=run.index, round_=0, text=run.summary or NO_TEXT)
        for follow in record.followups:
            if follow.step == step and follow.outcome == "completed" and follow.dispatch_id:
                await self._append(owner, record, key=f"pub:{follow.dispatch_id}",
                                   author="member", member=follow.target, round_=follow.round,
                                   text=follow.summary or NO_TEXT)

    async def _ask(self, record: GroupRecord, board: tuple[GroupMessage, ...],
                   message: GroupMessage, target: int) -> FollowUp:
        """为一次追问派工；被问成员只拿到已发布内容与追问原文。"""
        members = record.plan.steps[record.step].members
        published = tuple(m for m in board if m.step == record.step)
        brief = followup_brief(members[target].brief, published, message, members)
        request_id = f"grp:{record.group_id}:{record.step}:{target}:f{message.seq}"
        dispatch_id, outcome, reason = await self._start(record, target, brief, request_id)
        return FollowUp(step=record.step, target=target, asked_by=message.seq,
                        round=message.round + 1, dispatch_id=dispatch_id, outcome=outcome,
                        end_reason=reason)

    async def _append(self, owner: TraceOwner, record: GroupRecord, *, key: str, author: str,
                      member: int | None, round_: int, text: str) -> None:
        """按键幂等追加一条消息；成员消息解析 @。"""
        members = record.plan.steps[record.step].members
        mentions = parse_mentions(text, members, member) if author == "member" else ()
        await self._messages.append(owner, GroupMessage(
            group_id=record.group_id, tenant_id=record.tenant_id, user_id=record.user_id,
            key=key, author=author, step=record.step, member=member,  # type: ignore[arg-type]
            round=round_, mentions=mentions, text=text[:3000],
            created_at=datetime.fromtimestamp(self._now(), UTC)))
