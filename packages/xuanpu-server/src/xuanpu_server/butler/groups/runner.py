"""编组执行：提交计划、按步推进（并行 / 接力）、取消、全部收口后汇总一次。

推进是幂等的「看一眼、做一步、存一次」：
- 成员派工的幂等标识是 ``grp:<编组>:<步>:<成员>``，进程在派工与保存之间被终止，重启后
  再推进只会回放原派工，不会重复派出或重复启动。
- 成员结果来自派工记录的真实终态；成员的结果摘要由唤醒处理写入（``record_result``），
  成员不单独向员工交付。
- 会诊（R5-D1）：一步全部完成后先比对并保存裁决，高分歧时建裁决项并等员工裁决，裁决后
  才进入下一步或收口；裁决项与消息都按键去重。
- 汇总消息按 ``butler:group:<编组>:summary`` 写进主对话，同一标识只写一次；写完才标记
  已汇总，进程在两者之间被终止，重启后补标记，不重复汇总。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from xuanpu_server.butler.budget import ButlerBudgetError, TraceUsage
from xuanpu_server.butler.dispatcher import LaunchContext
from xuanpu_server.butler.groups.consult import ConsultCoordinator, parse_ruling_key
from xuanpu_server.butler.groups.plan import (
    GROUP_TERMINAL,
    SUMMARY_MAX,
    GroupRecord,
    MemberRun,
    final_state,
    group_fingerprint,
    next_step,
    step_outcome,
    with_run,
    with_verdict,
)
from xuanpu_server.butler.groups.summary import relay_brief, summary_text
from xuanpu_server.butler.groups.trace import group_span
from xuanpu_server.butler.groups.validate import check_group_budget, check_members, parse_plan
from xuanpu_server.butler.models import FINISHED_STATES
from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    PlatformError,
)
from xuanpu_server.ids import new_id
from xuanpu_server.tenancy import Principal
from xuanpu_server.tracing.models import TraceOwner

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.butler.dispatcher import Dispatcher
    from xuanpu_server.butler.groups.divergence import DivergenceRater
    from xuanpu_server.butler.groups.trace import TracedGroupStore
    from xuanpu_server.butler.models import DispatchRecord
    from xuanpu_server.butler.turn import ButlerTurn
    from xuanpu_server.butler.wake_models import PendingItem
    from xuanpu_server.platform import Platform

logger = logging.getLogger(__name__)
_SAVE_RETRIES = 5


def employee(owner: TraceOwner) -> Principal:
    """归属 → 员工身份（后台推进以本人身份派工）。"""
    return Principal(role="tenant_member", tenant_id=owner.tenant_id, user_id=owner.user_id,
                     key_id=None)


class GroupRunner:
    """编组的提交、推进、取消与结果登记。"""

    def __init__(self, platform: Platform, dispatcher: Dispatcher, groups: TracedGroupStore, *,
                 now: Callable[[], float], rater: DivergenceRater | None = None) -> None:
        """依赖由平台注入；``rater`` 是会诊比对的模型评级（None 时会诊一律交员工裁决）。"""
        self._platform = platform
        self._dispatcher = dispatcher
        self._groups = groups
        self._now = now
        self._consult = ConsultCoordinator(platform, rater, now=now)

    def use_rater(self, rater: DivergenceRater) -> None:
        """装配时补上模型评级（评级要用管家引擎，引擎晚于本对象构造）。"""
        self._consult = ConsultCoordinator(self._platform, rater, now=self._now)

    def _at(self) -> datetime:
        """当前时间（UTC）。"""
        return datetime.fromtimestamp(self._now(), UTC)

    async def submit(self, turn: ButlerTurn, raw_plan: Any) -> GroupRecord:
        """校验计划、建编组并立即推进第一步；同一输入重试时回放原编组。"""
        config = self._platform.butler_config
        plan = parse_plan(raw_plan, config)
        await check_members(plan, turn.who, self._platform.artisans, turn.materials)
        owner = turn.owner()
        request_id = f"{turn.request_id}:g{len(turn.groups) + 1}"
        if await self._groups.find_by_request(owner, request_id) is None:
            # 重试回放原编组时不再核预算（原编组的成员已计入）
            existing = await self._platform.dispatches.list_for_trace(owner, turn.trace_id)
            tokens = await self._dispatcher.finished_tokens(owner, turn.trace_id)
            check_group_budget(config, plan, TraceUsage(artisans=len(existing), tokens=tokens))
        if turn.root_span is None:
            raise ConflictError("管家根执行尚未开始，稍后重试")
        now = self._at()
        record = GroupRecord(
            group_id=new_id("grp"), tenant_id=owner.tenant_id, user_id=owner.user_id,
            request_id=request_id, input_fingerprint=group_fingerprint(turn.trace_id, plan),
            trace_id=turn.trace_id, parent_span_id=turn.root_span, timezone=turn.timezone,
            plan=plan, created_at=now, updated_at=now)
        created, _ = await self._groups.create(owner, record)
        turn.groups.append(created.group_id)
        return await self.advance(owner, created.group_id)

    async def get(self, owner: TraceOwner, group_id: str) -> GroupRecord:
        """读一个本人的编组。"""
        return await self._groups.get(owner, group_id)

    async def advance(self, owner: TraceOwner, group_id: str) -> GroupRecord:
        """推进到无事可做为止；每次只做一步并保存。"""
        record = await self._groups.get(owner, group_id)
        limit = record.plan.member_count() * 3 + len(record.plan.steps) * 2 + 4
        for _ in range(limit):
            updated = await self._step(owner, record)
            if updated is None:
                return record
            record = updated
        raise RuntimeError(f"编组 {group_id} 推进次数超过上限")

    async def advance_tenant(self, tenant_id: str) -> None:
        """推进租户内全部未收口的编组；单个失败记日志，下一轮重试。"""
        for record in await self._groups.active(tenant_id):
            owner = TraceOwner(tenant_id=record.tenant_id, user_id=record.user_id)
            try:
                await self.advance(owner, record.group_id)
            except Exception:
                logger.exception("推进编组 %s 失败，下一轮重试", record.group_id)

    async def cancel(self, owner: TraceOwner, group_id: str) -> GroupRecord:
        """登记取消并推进：未启动的成员不再启动，进行中的成员请求停止，按真实终态收敛。"""
        for _ in range(_SAVE_RETRIES):
            record = await self._groups.get(owner, group_id)
            if record.state in GROUP_TERMINAL or record.cancel_requested:
                return await self.advance(owner, group_id)
            try:
                await self._save(owner, record.model_copy(update={"cancel_requested": True}))
            except ConflictError:
                continue
            return await self.advance(owner, group_id)
        raise ConflictError("编组正被频繁更新，请稍后再取消")

    async def record_result(self, owner: TraceOwner, dispatch: DispatchRecord, *,
                            passed: bool | None, summary: str | None) -> None:
        """唤醒处理验收一个成员后登记摘要与验收结论（结果仍以派工终态为准）。"""
        assert dispatch.group_id is not None
        for _ in range(_SAVE_RETRIES):
            record = await self._groups.get(owner, dispatch.group_id)
            run = record.by_dispatch(dispatch.dispatch_id)
            if run is None or (run.summary, run.passed) == (summary, passed):
                return
            text = None if summary is None else summary[:SUMMARY_MAX]
            try:
                await self._save(owner, with_run(record, run.model_copy(
                    update={"summary": text, "passed": passed})))
                return
            except ConflictError:
                continue
        raise ConflictError("编组正被频繁更新，成员结果稍后重试登记")

    async def _save(self, owner: TraceOwner, record: GroupRecord) -> GroupRecord:
        """带期望版本保存。"""
        stamped = record.model_copy(update={"updated_at": self._at()})
        return await self._groups.save(owner, stamped, expected_version=record.version)

    async def _step(self, owner: TraceOwner, record: GroupRecord) -> GroupRecord | None:
        """做一步；没有可做的返回 None。"""
        if record.state in GROUP_TERMINAL:
            return None if record.summarized else await self._summarize(owner, record)
        refreshed = await self._refresh(owner, record)
        if refreshed is not record:
            return await self._save(owner, refreshed)
        if record.cancel_requested:
            return await self._cancel_step(owner, record)
        launched = await self._launch_next(owner, record)
        if launched is not None:
            return await self._save(owner, launched)
        outcome = step_outcome(record, record.step)
        if outcome is None:
            return None
        if self._needs_verdict(record, outcome):
            return await self._judge(owner, record)
        nxt, outcomes = next_step(record, outcome)
        if nxt is None:
            return await self._save(owner, record.model_copy(
                update={"state": final_state(outcomes), "step_outcomes": outcomes}))
        return await self._save(owner, record.model_copy(
            update={"step": nxt, "step_outcomes": outcomes}))

    @staticmethod
    def _needs_verdict(record: GroupRecord, outcome: str) -> bool:
        """会诊一步全部完成后，还没比对或还在等员工裁决。"""
        if record.plan.steps[record.step].mode != "consult" or outcome != "completed":
            return False
        verdict = record.verdict(record.step)
        return verdict is None or verdict.waiting

    async def _judge(self, owner: TraceOwner, record: GroupRecord) -> GroupRecord | None:
        """比对并保存；高分歧时建裁决项并保存其标识；已在等裁决时无事可做。"""
        verdict = record.verdict(record.step)
        if verdict is None:
            verdict = await self._consult.judge(owner, record)
            return await self._save(owner, with_verdict(record, verdict))
        if verdict.pending_id is None:
            verdict = await self._consult.open_ruling(owner, record, verdict)
            return await self._save(owner, with_verdict(record, verdict))
        return None

    async def rule(self, owner: TraceOwner, item: PendingItem, text: str,
                   request_id: str) -> None:
        """记下员工对一步会诊的裁决并推进编组；同一请求重试幂等，已有不同裁决报冲突。"""
        group_id, step = parse_ruling_key(item.source_key)
        for _ in range(_SAVE_RETRIES):
            record = await self._groups.get(owner, group_id)
            verdict = record.verdict(step)
            if verdict is None or verdict.pending_id != item.pending_id:
                raise ConflictError("这项裁决已不再需要")
            if verdict.ruling is not None:
                if (verdict.ruling, verdict.ruling_request_id) != (text, request_id):
                    raise ConflictError("这次会诊已经裁决过")
                break
            if record.state in GROUP_TERMINAL:
                raise ConflictError("编组已结束，不再需要裁决")
            ruled = verdict.model_copy(update={"ruling": text, "ruling_request_id": request_id,
                                               "ruled_at": self._at()})
            try:
                await self._save(owner, with_verdict(record, ruled))
                break
            except ConflictError:
                continue
        else:
            raise ConflictError("编组正被频繁更新，请稍后再裁决")
        await self.advance(owner, group_id)

    async def _refresh(self, owner: TraceOwner, record: GroupRecord) -> GroupRecord:
        """把已到终态的成员派工结果写进编组；没有变化时原样返回同一对象。"""
        updated = record
        for run in record.runs:
            if run.outcome is not None or run.dispatch_id is None:
                continue
            dispatch = await self._platform.dispatches.get(owner, run.dispatch_id)
            if dispatch.state in FINISHED_STATES:
                updated = with_run(updated, run.model_copy(update={
                    "outcome": dispatch.state, "end_reason": dispatch.end_reason}))
        return updated

    async def _launch_next(self, owner: TraceOwner, record: GroupRecord) -> GroupRecord | None:
        """启动当前步下一个该启动的成员；没有返回 None。"""
        step = record.plan.steps[record.step]
        for index, _member in enumerate(step.members):
            if record.run(record.step, index) is not None:
                continue
            if step.mode == "relay" and index > 0:
                previous = record.run(record.step, index - 1)
                if previous is None or previous.outcome != "completed":
                    return None
            return with_run(record, await self._launch(owner, record, index))
        return None

    async def _launch(self, owner: TraceOwner, record: GroupRecord, index: int) -> MemberRun:
        """派出一个成员；匠人对本人不可用（撤权）只拒这个成员。"""
        member = record.plan.steps[record.step].members[index]
        brief = member.brief
        if record.plan.steps[record.step].mode == "relay" and index > 0:
            brief = await self._relay_brief(owner, record, index)
        ctx = LaunchContext(who=employee(owner), owner=owner, trace_id=record.trace_id,
                            parent_span_id=group_span(record.group_id),
                            request_id=f"grp:{record.group_id}:{record.step}:{index}",
                            timezone=record.timezone)
        base = MemberRun(step=record.step, index=index)
        try:
            dispatch = await self._dispatcher.launch(
                ctx, package=member.package, entry=member.entry, instruction=brief,
                materials=member.materials, group_id=record.group_id)
        except (NotFoundError, PermissionDeniedError):
            return base.model_copy(update={"outcome": "failed", "end_reason": "member_unavailable"})
        except ButlerBudgetError:
            return base.model_copy(update={"outcome": "failed", "end_reason": "budget_exceeded"})
        except InvalidRequestError:
            return base.model_copy(update={"outcome": "failed", "end_reason": "invalid_member"})
        except PlatformError:
            started = await self._by_request(owner, record.trace_id, ctx.request_id)
            if started is None:
                return base.model_copy(update={"outcome": "failed", "end_reason": "start_failed"})
            return base.model_copy(update={"dispatch_id": started.dispatch_id})
        return base.model_copy(update={"dispatch_id": dispatch.dispatch_id})

    async def _relay_brief(self, owner: TraceOwner, record: GroupRecord, index: int) -> str:
        """接力：下一成员拿到前一成员的成果引用与摘要；引用须属于本人（服务端复核）。"""
        previous = record.run(record.step, index - 1)
        assert previous is not None and previous.dispatch_id is not None
        source = await self._platform.dispatches.get(owner, previous.dispatch_id)
        member = record.plan.steps[record.step].members[index]
        return relay_brief(member.brief, source, previous.summary)

    async def _by_request(self, owner: TraceOwner, trace_id: str,
                          request_id: str) -> DispatchRecord | None:
        """按幂等标识找已建的成员派工。"""
        for dispatch in await self._platform.dispatches.list_for_trace(owner, trace_id):
            if dispatch.request_id == request_id:
                return dispatch
        return None

    async def _cancel_step(self, owner: TraceOwner, record: GroupRecord) -> GroupRecord | None:
        """取消：未启动的成员记为取消；进行中的请求停止；全部有结果后编组记为取消。"""
        updated = record
        for step_index in range(record.step, len(record.plan.steps)):
            for index in range(len(record.plan.steps[step_index].members)):
                if updated.run(step_index, index) is None:
                    updated = with_run(updated, MemberRun(step=step_index, index=index,
                                                          outcome="cancelled",
                                                          end_reason="group_cancelled"))
        pending = [r for r in updated.runs if r.outcome is None and r.dispatch_id is not None]
        for run in pending:
            assert run.dispatch_id is not None
            await self._stop(owner, run.dispatch_id)
        if pending:
            return None if updated is record else await self._save(owner, updated)
        await self._close_rulings(owner, record)
        done = len(record.step_outcomes)
        outcomes = (*record.step_outcomes, *("cancelled",) * (len(record.plan.steps) - done))
        return await self._save(owner, updated.model_copy(
            update={"state": "cancelled", "step_outcomes": outcomes}))

    async def _close_rulings(self, owner: TraceOwner, record: GroupRecord) -> None:
        """编组取消时，仍在等员工的裁决项一并关闭（可重做）。"""
        store = self._platform.store.wake_store
        for verdict in record.verdicts:
            if verdict.pending_id is None or not verdict.waiting:
                continue
            item = await store.get_pending(owner, verdict.pending_id)
            if item.state == "open":
                await store.update_pending(owner, item.pending_id, expected_version=item.version,
                                           at=self._at(), state="cancelled")

    async def _stop(self, owner: TraceOwner, dispatch_id: str) -> None:
        """请求宿主停止（沿用 R2 口径，可重复请求）；终态以宿主实际结果为准。"""
        dispatch = await self._platform.dispatches.get(owner, dispatch_id)
        if dispatch.host_ref is None or not dispatch.active:
            return
        who = employee(owner)
        if dispatch.host == "task":
            await self._platform.work_tasks.cancel(who, dispatch.host_ref)
        else:
            await self._platform.sessions.cancel(who, dispatch.host_ref)

    async def _summarize(self, owner: TraceOwner, record: GroupRecord) -> GroupRecord:
        """写一次汇总消息（同一标识只写一次），再标记已汇总。"""
        packages = await self._member_names(owner, record)
        await self._platform.workspace.append_message(
            employee(owner), "assistant", summary_text(record, packages),
            f"butler:group:{record.group_id}:summary",
            metadata={"butler": {"trace_id": record.trace_id, "group_id": record.group_id,
                                 "event": "group_summary", "state": record.state}})
        return await self._save(owner, record.model_copy(update={"summarized": True}))

    async def _member_names(self, owner: TraceOwner, record: GroupRecord) -> dict[str, str]:
        """成员派工 → 给员工看的匠人名。"""
        names: dict[str, str] = {}
        for run in record.runs:
            if run.dispatch_id is not None:
                dispatch = await self._platform.dispatches.get(owner, run.dispatch_id)
                names[run.dispatch_id] = dispatch.artisan.package
        return names
