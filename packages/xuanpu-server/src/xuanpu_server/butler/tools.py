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
from xuanpu_server.butler.groups.tools import GROUP_PLAN_SCHEMA
from xuanpu_server.errors import PlatformError
from xuanpu_server.work_tasks.models import InputFile

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from xuanpu_server.butler.dispatcher import Dispatcher
    from xuanpu_server.butler.groups.runner import GroupRunner
    from xuanpu_server.butler.pending import PendingService
    from xuanpu_server.butler.turn import ButlerTurn, TurnRegistry
    from xuanpu_server.platform import Platform

    Handler = Callable[[ButlerTurn, dict[str, Any], str], Awaitable[taifeng.ToolResult]]

TOOL_NAMES = ("propose", "read_personal", "list_artisans", "dispatch_artisan", "check_dispatch",
              "cancel_dispatch", "answer_pending", "answer_artisan", "ask_user",
              "dispatch_group", "cancel_group", "check_group")
_WAKE_ONLY = frozenset({"answer_artisan", "ask_user"})
_ANY_TURN = frozenset({"read_personal", "list_artisans", "check_dispatch", "check_group"})
_LIST_MAX = 20


def _json(data: object) -> str:
    """工具输出统一成 JSON 文本。"""
    return json.dumps(data, ensure_ascii=False, default=str)


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
            self._spec("propose", "提出一个平台内置能力的待确认草稿（提醒、待办、日程、事项、文件夹、"
                       "执行安排、交接、共享、业务跟进）或打开对应列表。草稿须员工本人确认。",
                       {"choice": {"type": "object", "description":
                                   "与内置能力选择同结构：package、entry 及对应草稿字段"}},
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
        ]

    def _group_specs(self) -> list[taifeng.ToolSpec]:
        """编组（R5-D）：提交计划、取消、查进展。"""
        from xuanpu_server.butler.groups.tools import GroupTools

        handlers = GroupTools(self._groups)
        return [
            self._spec("dispatch_group", "需要多位匠人时提交一份编组计划：每步并行（parallel，各做一部分）"
                       "或接力（relay，前一位的成果摘要交给后一位）；立即返回，不等结果。",
                       {"plan": GROUP_PLAN_SCHEMA}, ["plan"], handlers.dispatch_group),
            self._spec("cancel_group", "请求停止一个编组的全部未结束成员；以匠人实际停下为准。",
                       {"group_id": {"type": "string"}}, ["group_id"], handlers.cancel_group),
            self._spec("check_group", "查一个编组的进展。",
                       {"group_id": {"type": "string"}}, ["group_id"], handlers.check_group,
                       parallel=True),
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
            if (name in _WAKE_ONLY) != (turn.wake is not None) and name not in _ANY_TURN:
                return taifeng.ToolResult.error(
                    "这个工具只在匠人结束后被唤醒时使用" if name in _WAKE_ONLY
                    else "被唤醒复核时不能新建草稿或派工")
            try:
                return await handler(turn, args, ctx.call_id)
            except PlatformError as exc:
                return taifeng.ToolResult.error(f"{exc.code}: {exc.message}")
            except (ValidationError, ValueError) as exc:
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
            return taifeng.ToolResult.error("propose 只用于平台内置能力；企业业务请交给匠人")
        outcome = await resolve_native_choice(
            self._platform, turn.who, choice, now=self._now(), request_id=turn.request_id,
            input_files=turn.materials)
        if outcome is None:
            return taifeng.ToolResult.error("这个内置能力当前不可用")
        turn.proposal = outcome
        return taifeng.ToolResult.ok(_json({"card": outcome.message, "needs_confirmation": True}))

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

    async def _check_dispatch(self, turn: ButlerTurn, args: dict[str, Any], _call: str,
                              ) -> taifeng.ToolResult:
        """本人派工的状态；他人的与不存在的一样不可见。"""
        record = await self._platform.dispatches.get(turn.owner(),
                                                               str(args["dispatch_id"]))
        return taifeng.ToolResult.ok(_json({"dispatch_id": record.dispatch_id,
                                            "state": record.state,
                                            "artisan": record.artisan.package}))
