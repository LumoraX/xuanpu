"""管家的平台工具：提案、只读查询、列匠人、派工、查派工。

没有任何业务包的连接器工具，没有文件、命令或网络工具。身份一律取自本轮上下文
（按内核线程标识找回），不取自模型参数；工具结果交给模型时都是数据。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import taifeng
from pydantic import ValidationError

from xuanpu_server.assistant_proposals import NATIVE_PACKAGES, resolve_native_choice
from xuanpu_server.assistant_reminders import REMINDER_PROMPT
from xuanpu_server.assistant_task_definitions import TASK_DEFINITION_PROMPT
from xuanpu_server.assistant_workspace import WORKSPACE_PROMPT
from xuanpu_server.butler.groups.tools import GROUP_PLAN_SCHEMA
from xuanpu_server.collaboration.routing import COLLABORATION_PROMPT
from xuanpu_server.errors import PlatformError
from xuanpu_server.proactive.models import PROACTIVE_ROUTING
from xuanpu_server.shared_matters.routing import SHARED_PROMPT
from xuanpu_server.work_tasks.models import InputFile

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from xuanpu_server.assistant_reminders import RouteOutcome
    from xuanpu_server.butler.dispatcher import Dispatcher
    from xuanpu_server.butler.groups.runner import GroupRunner
    from xuanpu_server.butler.pending import PendingService
    from xuanpu_server.butler.turn import ButlerTurn, TurnRegistry
    from xuanpu_server.platform import Platform

    Handler = Callable[[ButlerTurn, dict[str, Any], str], Awaitable[taifeng.ToolResult]]

TOOL_NAMES = ("propose", "read_personal", "list_artisans", "dispatch_artisan", "check_dispatch",
              "cancel_dispatch", "answer_pending", "answer_artisan", "ask_user",
              "dispatch_group", "cancel_group", "check_group", "rate_divergence",
              "extend_turn")
_WAKE_ONLY = frozenset({"answer_artisan", "ask_user"})
_ANY_TURN = frozenset({"read_personal", "list_artisans", "check_dispatch", "check_group",
                       "extend_turn"})
_LIST_MAX = 20
_ERRORS_MAX = 8
# propose 的 choice 结构：与分流共用同一份内置能力约定（包名带 @，草稿字段名固定），
# 不另写一份，避免两处漂移。真实模型验收发现缺了它时模型只能猜包名与字段。
PROPOSE_DESCRIPTION = (
    "提出一个平台内置能力的待确认草稿（提醒、待办、日程、事项、文件夹、执行安排、交接、共享、"
    "业务跟进）或打开对应列表。草稿须员工本人确认。choice 只含 package、entry、message 与下列"
    "草稿字段之一；package 与 entry 必须用下列约定里的准确标识（带 @），message 填 null，"
    "给员工的话写在你自己的回复里。约定如下：\n"
    + REMINDER_PROMPT + WORKSPACE_PROMPT + TASK_DEFINITION_PROMPT + PROACTIVE_ROUTING
    + SHARED_PROMPT + COLLABORATION_PROMPT
    # 企业连接在分流里是目录条目而非提示词，这里补一句同义约定
    + "\n本人企业只读连接（绑定企业微信、查看或撤销授权）选择@connections/manage，只打开本人连接页；"
    "企业未开通时平台会拒绝，如实说明即可。"
)


def _refusal(name: str, turn: ButlerTurn) -> str | None:
    """这一轮不允许调用该工具时给出原因；允许返回 None。"""
    if turn.compare is not None:
        return None if name == "rate_divergence" else "会诊比对时只能登记分歧度"
    if name == "rate_divergence":
        return "这个工具只在会诊比对时使用"
    if (name in _WAKE_ONLY) != (turn.wake is not None) and name not in _ANY_TURN:
        return ("这个工具只在匠人结束后被唤醒时使用" if name in _WAKE_ONLY
                else "被唤醒复核时不能新建草稿或派工")
    return None


def _json(data: object) -> str:
    """工具输出统一成 JSON 文本。"""
    return json.dumps(data, ensure_ascii=False, default=str)


def has_draft(outcome: RouteOutcome) -> bool:
    """是否带待确认草稿（只打开列表的不算）。"""
    return any((outcome.reminder_draft, outcome.workspace_draft, outcome.proactive_draft,
                outcome.task_definition_draft, outcome.shared_draft, outcome.handoff_draft))


def _validation_summary(exc: ValidationError) -> str:
    """参数校验错误：只给字段位置与原因，不回显取值，便于模型自行改正。"""
    errors = exc.errors(include_url=False, include_input=False, include_context=False)
    parts = [f"{'.'.join(str(p) for p in e['loc']) or '(根)'}: {e['msg']}" for e in errors[:_ERRORS_MAX]]
    if len(errors) > _ERRORS_MAX:
        parts.append(f"另有 {len(errors) - _ERRORS_MAX} 处")
    return "; ".join(parts)


class ButlerTools:
    """构造管家工具；处理函数共用平台、派工与本轮登记表。"""

    def __init__(self, platform: Platform, dispatcher: Dispatcher, turns: TurnRegistry, *,
                 pending: PendingService, groups: GroupRunner, now: Callable[[], float]) -> None:
        """依赖由平台注入。"""
        self._groups = groups
        self._platform = platform
        self._pending = pending
        self._dispatcher = dispatcher
        self._turns = turns
        self._now = now

    def specs(self) -> list[taifeng.ToolSpec]:
        """全部管家工具。"""
        return [
            self._spec("propose", PROPOSE_DESCRIPTION,
                       {"choice": {"type": "object", "description":
                                   "package、entry（按工具说明里的约定）、message=null 及对应草稿字段"}},
                       ["choice"], self._propose),
            self._spec("read_personal", "查本人的提醒或进行中的工作（只读）。",
                       {"kind": {"type": "string", "enum": ["reminders", "work_tasks"]}},
                       ["kind"], self._read_personal, parallel=True),
            self._spec("list_artisans", "列出本人此刻可用的匠人（企业业务能力）及其说明。",
                       {}, [], self._list_artisans, parallel=True),
            self._spec("dispatch_artisan", "把任务交给一位匠人；立即返回，不等结果。",
                       {"package": {"type": "string"}, "entry": {"type": "string"},
                        "instruction": {"type": "string", "description": "给匠人的任务说明"},
                        "materials": {"type": "array", "items": {
                            "type": "object", "properties": {
                                "item_id": {"type": "string"}, "revision": {"type": "integer"}},
                            "required": ["item_id", "revision"]}}},
                       ["package", "entry", "instruction"], self._dispatch),
            self._spec("check_dispatch", "查一次派工的当前状态。",
                       {"dispatch_id": {"type": "string"}}, ["dispatch_id"],
                       self._check_dispatch, parallel=True),
            *self._c2_specs(),
            *self._group_specs(),
            *self._limit_specs(),
            self._spec("rate_divergence", "（仅会诊比对时）登记各位匠人结论的分歧度与综合。",
                       {"level": {"type": "string", "enum": ["low", "medium", "high"],
                                  "description": "low 措辞差异；medium 评估不同；high 结论矛盾"},
                        "synthesis": {"type": "string", "description": "综合后的结论（low/medium 必填）"},
                        "differences": {"type": "array", "items": {"type": "string"},
                                        "description": "不一致之处（medium 必列）"},
                        "basis": {"type": "array", "items": {"type": "integer"},
                                  "description": "综合所依据的成员序号"}},
                       ["level"], self._rate_divergence),
        ]

    def _group_specs(self) -> list[taifeng.ToolSpec]:
        """编组（R5-D）：提交计划、取消、查进展。"""
        from xuanpu_server.butler.groups.tools import GroupTools

        handlers = GroupTools(self._groups)
        return [
            self._spec("dispatch_group", "需要多位匠人时提交一份编组计划：每步并行（parallel，各做一部分）、"
                       "接力（relay，前一位的成果摘要交给后一位）或会诊（consult，就同一问题各自给结论，"
                       "全部给完后比对）；第一步的 when 写 null。立即返回，不等结果。",
                       {"plan": GROUP_PLAN_SCHEMA}, ["plan"], handlers.dispatch_group),
            self._spec("cancel_group", "请求停止一个编组的全部未结束成员；以匠人实际停下为准。",
                       {"group_id": {"type": "string"}}, ["group_id"], handlers.cancel_group),
            self._spec("check_group", "查一个编组的进展。",
                       {"group_id": {"type": "string"}}, ["group_id"], handlers.check_group,
                       parallel=True),
        ]

    def _limit_specs(self) -> list[taifeng.ToolSpec]:
        """管家调整自己的时限（R6-B）。"""
        from xuanpu_server.butler.limits import LimitTools

        handlers = LimitTools(self._platform)
        return [
            self._spec("extend_turn", "这件事你还需要更多时间亲自处理时，说明理由并延长本轮时限"
                       "（从现在起再给 seconds 秒，有上限）。长的工作优先拆成派工或编组。",
                       {"seconds": {"type": "integer", "minimum": 1},
                        "reason": {"type": "string", "description": "为什么还需要这些时间"}},
                       ["seconds", "reason"], handlers.extend_turn),
        ]

    def _c2_specs(self) -> list[taifeng.ToolSpec]:
        """取消、答复待处理项、代答与转问。"""
        from xuanpu_server.butler.wake_tools import WakeTools

        handlers = WakeTools(self._platform, self._pending)
        return [
            self._spec("cancel_dispatch", "请求停止一次派工；以匠人实际停下为准。",
                       {"dispatch_id": {"type": "string"}}, ["dispatch_id"], handlers.cancel),
            self._spec("answer_pending", "员工这句话是在答复某个待处理的问题时，把原话转给对应匠人。",
                       {"pending_id": {"type": "string"}}, ["pending_id"], handlers.answer_pending),
            self._spec("answer_artisan", "（仅被唤醒时）代员工答复匠人的提问：答复必须逐字取自出处原文。",
                       {"answer": {"type": "string"},
                        "source_kind": {"type": "string", "enum": ["setting", "conversation"]},
                        "quote": {"type": "string", "description": "出处里的原文片段"}},
                       ["answer", "source_kind", "quote"], handlers.answer_artisan),
            self._spec("ask_user", "（仅被唤醒时）把匠人的问题转问员工本人。",
                       {"question": {"type": "string"}}, ["question"], handlers.ask_user),
        ]

    def _spec(self, name: str, description: str, properties: dict[str, Any],
              required: list[str], handler: Handler, *, parallel: bool = False,
              ) -> taifeng.ToolSpec:
        """包一层：按线程找回本轮上下文；平台错误与参数错误作为工具错误交给模型。"""
        async def run(args: dict[str, Any], ctx: taifeng.ToolContext) -> taifeng.ToolResult:
            """执行一次工具调用。"""
            turn = self._turns.get(ctx.thread_id)
            if turn is None:
                return taifeng.ToolResult.error("管家上下文已失效，本次调用未执行")
            refusal = _refusal(name, turn)
            if refusal is not None:
                return taifeng.ToolResult.error(refusal)
            try:
                return await handler(turn, args, ctx.call_id)
            except PlatformError as exc:
                return taifeng.ToolResult.error(f"{exc.code}: {exc.message}")
            except ValidationError as exc:
                return taifeng.ToolResult.error(f"invalid_arguments: {_validation_summary(exc)}")
            except ValueError as exc:
                return taifeng.ToolResult.error(f"invalid_arguments: {type(exc).__name__}")

        schema = {"type": "object", "properties": properties, "required": required,
                  "additionalProperties": False}
        return taifeng.ToolSpec(name=name, description=description, input_schema=schema,
                                handler=run, parallel_safe=parallel, timeout_seconds=60.0)

    async def _propose(self, turn: ButlerTurn, args: dict[str, Any], _call: str) -> taifeng.ToolResult:
        """解析内置能力选择，产出与分流相同的待确认卡片；每轮最多一个。"""
        from xuanpu_server.assistant import Choice

        if turn.proposal is not None:
            return taifeng.ToolResult.error("本轮已经提出过一个草稿")
        choice = Choice.model_validate_json(json.dumps(args.get("choice")))
        if choice.package not in NATIVE_PACKAGES:
            # 以 @ 开头多半是把入口拼进了包名（如 @workspace/create）：说清合法取值，模型才能改正
            # （真实模型下出现过，旧提示「企业业务请交给匠人」让模型放弃了草稿）
            if (choice.package or "").startswith("@"):
                return taifeng.ToolResult.error(
                    "package 不对：只能是 " + "、".join(sorted(NATIVE_PACKAGES))
                    + " 之一，不含入口；入口（如 create、manage）单独写在 entry 里")
            return taifeng.ToolResult.error("propose 只用于平台内置能力；企业业务请交给匠人")
        outcome = await resolve_native_choice(
            self._platform, turn.who, choice, now=self._now(), request_id=turn.request_id,
            input_files=turn.materials)
        if outcome is None:
            return taifeng.ToolResult.error("这个内置能力当前不可用")
        turn.proposal = outcome
        # 只打开列表时不需要确认；如实告诉模型，免得它对员工说「请确认后查看」
        return taifeng.ToolResult.ok(_json({"card": outcome.message,
                                            "needs_confirmation": has_draft(outcome)}))

    async def _read_personal(self, turn: ButlerTurn, args: dict[str, Any], _call: str,
                             ) -> taifeng.ToolResult:
        """本人的提醒或工作，只给标题与状态。"""
        kind = args.get("kind")
        if kind == "reminders":
            records = await self._platform.reminders.list(turn.who, status="active")
            items = [{"title": r.title, "due_at": r.due_at, "status": r.status}
                     for r in records[:_LIST_MAX]]
        elif kind == "work_tasks":
            tasks = await self._platform.work_tasks.store.list(turn.who, limit=_LIST_MAX)
            items = [{"task_id": t.task_id, "package": t.package, "status": t.status}
                     for t in tasks]
        else:
            return taifeng.ToolResult.error("kind 只能是 reminders 或 work_tasks")
        return taifeng.ToolResult.ok(_json({"items": items}))

    async def _list_artisans(self, turn: ButlerTurn, _args: dict[str, Any], _call: str,
                             ) -> taifeng.ToolResult:
        """本人可用的匠人；说明文字是数据。"""
        artisans = await self._platform.artisans.available(turn.who)
        return taifeng.ToolResult.ok(_json({"artisans": [
            {"package": a.ref.package, "entry": a.ref.entry, "description": a.description,
             "entry_description": a.entry_description} for a in artisans]}))

    async def _dispatch(self, turn: ButlerTurn, args: dict[str, Any], _call: str,
                        ) -> taifeng.ToolResult:
        """派工并立即返回派工标识与状态。"""
        materials = tuple(InputFile.model_validate(m) for m in args.get("materials") or [])
        record = await self._dispatcher.dispatch(
            turn, package=str(args["package"]), entry=str(args["entry"]),
            instruction=str(args["instruction"]), materials=materials)
        return taifeng.ToolResult.ok(_json({"dispatch_id": record.dispatch_id,
                                            "state": record.state}))

    async def _rate_divergence(self, turn: ButlerTurn, args: dict[str, Any], _call: str,
                               ) -> taifeng.ToolResult:
        """登记比对评级；只登记一次，评级只决定走哪条路。"""
        from xuanpu_server.butler.groups.divergence import Rating, valid_rating

        compare = turn.compare
        assert compare is not None
        if compare.rating is not None:
            return taifeng.ToolResult.error("本轮已经登记过分歧度")
        rating = valid_rating(Rating.model_validate(args), compare.members)
        if rating is None:
            return taifeng.ToolResult.error("综合须非空且只能引用参与比对的成员序号")
        compare.rating = rating
        return taifeng.ToolResult.ok(_json({"recorded": rating.level}))

    async def _check_dispatch(self, turn: ButlerTurn, args: dict[str, Any], _call: str,
                              ) -> taifeng.ToolResult:
        """本人派工的状态；他人的与不存在的一样不可见。"""
        record = await self._platform.dispatches.get(turn.owner(),
                                                               str(args["dispatch_id"]))
        return taifeng.ToolResult.ok(_json({"dispatch_id": record.dispatch_id,
                                            "state": record.state,
                                            "artisan": record.artisan.package}))
