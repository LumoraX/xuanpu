"""管家的平台工具：提案、只读查询、列匠人、派工、查派工。

没有任何业务包的连接器工具，没有文件、命令或网络工具。身份一律取自本轮上下文
（按内核线程标识找回），不取自模型参数；工具结果交给模型时都是数据。
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import taifeng
from pydantic import ValidationError

from server.core.errors import PlatformError
from server.domains.collaboration.routing import COLLABORATION_PROMPT
from server.domains.people.routing import PEOPLE_PROMPT
from server.domains.proactive.models import PROACTIVE_ROUTING
from server.domains.shared_matters.routing import SHARED_PROMPT
from server.domains.work_tasks.models import InputFile
from server.engine.assistant.proposals import NATIVE_PACKAGES, resolve_native_choice
from server.engine.assistant.reminders import REMINDER_PROMPT
from server.engine.assistant.task_definitions import TASK_DEFINITION_PROMPT
from server.engine.assistant.workspace import WORKSPACE_PROMPT
from server.engine.butler.groups.tools import GROUP_PLAN_SCHEMA
from server.engine.butler.limits import time_hint
from server.engine.butler.personal_actions import execute_personal_action, read_extended_personal
from spec.localization import display_name_for

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from server.engine.assistant.reminders import RouteOutcome
    from server.engine.butler.dispatcher import Dispatcher
    from server.engine.butler.groups.runner import GroupRunner
    from server.engine.butler.pending import PendingService
    from server.engine.butler.turn import ButlerTurn, TurnRegistry
    from server.platform import Platform

    Handler = Callable[[ButlerTurn, dict[str, Any], str], Awaitable[taifeng.ToolResult]]

TOOL_NAMES = ("propose", "act_personal", "read_personal", "list_artisans", "dispatch_artisan", "check_dispatch",
              "cancel_dispatch", "answer_pending", "answer_artisan", "ask_user",
              "dispatch_group", "cancel_group", "check_group", "rate_divergence",
              "extend_turn", "raise_budget", "update_task_board")
_WAKE_ONLY = frozenset({"answer_artisan", "ask_user"})
_ANY_TURN = frozenset({"read_personal", "list_artisans", "check_dispatch", "check_group",
                       "extend_turn", "raise_budget", "update_task_board"})
_LIST_MAX = 20
_ERRORS_MAX = 8
# propose 的 choice 结构：与分流共用同一份内置能力约定（包名带 @，草稿字段名固定），
# 不另写一份，避免两处漂移。真实模型验收发现缺了它时模型只能猜包名与字段。
PROPOSE_DESCRIPTION = (
    "提出一个平台内置能力的待确认草稿（提醒、待办、日程、事项、文件夹、人物与关系/联系人名片、"
    "执行安排、交接、共享、业务跟进）或打开对应列表。草稿须员工本人确认。choice 只含 package、entry、message 与下列"
    "草稿字段之一；package 与 entry 必须用下列约定里的准确标识（带 @），message 填 null，"
    "给员工的话写在你自己的回复里。约定如下：\n"
    + REMINDER_PROMPT + WORKSPACE_PROMPT + PEOPLE_PROMPT + TASK_DEFINITION_PROMPT + PROACTIVE_ROUTING
    + SHARED_PROMPT + COLLABORATION_PROMPT
    # 企业连接在分流里是目录条目而非提示词，这里补一句同义约定
    + "\n本人企业只读连接（绑定企业微信、查看或撤销授权）选择@connections/manage，只打开本人连接页；"
    "企业未开通时平台会拒绝，如实说明即可。"
)


ACT_PERSONAL_DESCRIPTION = (
    "（仅本人私域自主办理，受个人设置 auto_execute_personal 控制，办完可一键撤回）"
    "直接代员工完成个人空间内的信息整理与办公操作，同一轮可连续调用多次，不限于单个草稿。"
    "严禁用于对外发送、组织审批、交接、共享或企业外部业务。\n"
    "支持 action：\n"
    "- capture_bundle：把随手记、会议纪要、名片或上传的文档内容一次性自动拆解并落库为原始记录+公司+人物+事项+待办+日程+提醒+跨实体关系（传 bundle，结构与 people_draft 完全一致，时间支持 UNIX 秒或 ISO-8601）；\n"
    "- create_todo / update_todo / complete_todo / reopen_todo / delete_todo：新建、编辑、勾选完成、重开或软删除本人待办（传 title/description/due_at/matter_id 或 item_id，均支持一键撤回）；\n"
    "- create_event / update_event / cancel_event：安排、修改或取消本人日程（传 title/start_at/end_at/timezone/description/matter_id 或 item_id，自动返回冲突与 60 分钟内紧邻日程，取消可一键恢复）；\n"
    "- create_reminder / cancel_reminder：设置或取消本人提醒（传 title/due_at/timezone/repeat 或 reminder_id）；\n"
    "- create_matter / update_matter：新建或更新本人项目事项（传 title/description/status 或 item_id）；\n"
    "- act_followup：处理本人主动感知跟进（传 followup_id、followup_action=complete|postpone|ignore_stage|set_reminder|progress 及可选 due_at）；\n"
    "- save_note_file：把你整理好的会议纪要、项目全景简报、周报或方案保存为本人工作台 Markdown 文件（传 name/content，可选 matter_id/parent_id）；\n"
    "- patch_personal_file：对本人已有 UTF-8 文本/Markdown 文件做精准局部块替换（传 item_id、edits=[{target, replacement}] 及可选 expected_version，严格唯一锚定并返回 Unified Diff，支持一键撤回）。"
)


def _refusal(name: str, turn: ButlerTurn) -> str | None:
    """这一轮不允许调用该工具时给出原因；允许返回 None。"""
    if turn.compare is not None:
        return None if name == "rate_divergence" else "会诊比对时只能登记分歧度"
    if name == "rate_divergence":
        return "这个工具只在会诊比对时使用"
    if name == "act_personal" and turn.wake is not None:
        return None if turn.wake.passed else "匠人验收未通过时不能自动写入个人空间"
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
                outcome.task_definition_draft, outcome.shared_draft, outcome.handoff_draft,
                outcome.people_draft))


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
            self._spec(
                "act_personal",
                ACT_PERSONAL_DESCRIPTION,
                {
                    "action": {
                        "type": "string",
                        "enum": [
                            "capture_bundle",
                            "create_todo",
                            "update_todo",
                            "complete_todo",
                            "reopen_todo",
                            "delete_todo",
                            "create_event",
                            "update_event",
                            "cancel_event",
                            "create_reminder",
                            "cancel_reminder",
                            "create_matter",
                            "update_matter",
                            "act_followup",
                            "save_note_file",
                            "patch_personal_file",
                        ],
                    },
                    "bundle": {"type": ["object", "null"], "description": "capture_bundle 时的复合整理包（结构同 people_draft）"},
                    "item_id": {"type": ["string", "null"], "description": "待办/日程/事项的 ID（更新或完成时必填）"},
                    "reminder_id": {"type": ["string", "null"], "description": "cancel_reminder 时的提醒 ID"},
                    "followup_id": {"type": ["string", "null"], "description": "act_followup 时的跟进 ID"},
                    "followup_action": {
                        "type": ["string", "null"],
                        "enum": ["complete", "postpone", "ignore_stage", "set_reminder", "progress", None],
                        "description": "act_followup 的处理动作",
                    },
                    "title": {"type": ["string", "null"], "description": "标题"},
                    "name": {"type": ["string", "null"], "description": "save_note_file 的文件名（如 2026-11-25-会议纪要.md）"},
                    "content": {"type": ["string", "null"], "description": "save_note_file 的 UTF-8 文档正文"},
                    "description": {"type": ["string", "null"], "description": "详细说明"},
                    "status": {"type": ["string", "null"], "description": "待办状态 open/done 或事项状态 active/completed/archived"},
                    "due_at": {"type": ["number", "string", "null"], "description": "截止或提醒时间（UNIX 秒或 ISO-8601）"},
                    "start_at": {"type": ["number", "string", "null"], "description": "日程开始时间（UNIX 秒或 ISO-8601）"},
                    "end_at": {"type": ["number", "string", "null"], "description": "日程结束时间（UNIX 秒或 ISO-8601）"},
                    "timezone": {"type": ["string", "null"], "description": "IANA 时区，默认沿用员工当前时区"},
                    "repeat": {"type": ["string", "null"], "enum": ["once", "daily", "weekly", None]},
                    "matter_id": {"type": ["string", "null"], "description": "关联的本人事项 ID"},
                    "parent_id": {"type": ["string", "null"], "description": "save_note_file 可选父目录 ID"},
                    "edits": {
                        "type": ["array", "null"],
                        "items": {
                            "type": "object",
                            "properties": {
                                "target": {"type": "string"},
                                "replacement": {"type": "string"},
                            },
                            "required": ["target", "replacement"],
                        },
                        "description": "patch_personal_file 的精确局部替换列表",
                    },
                    "expected_version": {"type": ["integer", "null"], "minimum": 1, "description": "patch_personal_file 可选 CAS 版本号"},
                },
                ["action"],
                self._act_personal,
            ),
            self._spec(
                "read_personal",
                "查本人的今日全景（today）、待办（todos）、日程（events）、提醒（reminders）、最近匠人派工（dispatches）、工作任务（work_tasks）、个人文件清单与文本正文（files/file_content）、个人知识库与记忆（knowledge）、主动业务跟进（followups）、以及个人知识图谱（orgs_search/people_search/matters/records_search/entity_graph/person_detail/matter_people）（全部只读）。",
                {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "today",
                            "todos",
                            "events",
                            "reminders",
                            "dispatches",
                            "files",
                            "file_content",
                            "knowledge",
                            "followups",
                            "work_tasks",
                            "matters",
                            "orgs_search",
                            "records_search",
                            "entity_graph",
                            "people_search",
                            "person_detail",
                            "matter_people",
                        ],
                    },
                    "query": {"type": ["string", "null"], "description": "关键词（todos/events/files/knowledge/people_search/orgs_search/matters/records_search）；查全部或最近时留空或填 null"},
                    "status": {"type": ["string", "null"], "description": "todos 状态过滤（open/done/all）"},
                    "item_id": {"type": ["string", "null"], "description": "file_content 要读取的本人文件 ID"},
                    "revision": {"type": ["integer", "null"], "minimum": 1, "description": "file_content 可选版本号"},
                    "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 50, "description": "最多返回条数"},
                    "organization": {"type": ["string", "null"], "description": "people_search 的公司过滤；不筛选时留空或填 null"},
                    "tag": {"type": ["string", "null"], "description": "people_search 或 orgs_search 的标签过滤；不筛选时留空或填 null"},
                    "birth_month": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "maximum": 12,
                        "description": "按生日月份筛选（1–12）；不按生日月份筛选时必须填 0 或 null",
                    },
                    "person_id": {"type": ["string", "null"], "description": "person_detail 的人物标识"},
                    "matter_id": {"type": ["string", "null"], "description": "todos/events/files/matter_people/records_search 的本人事项标识"},
                    "org_id": {"type": ["string", "null"], "description": "records_search 的公司标识过滤"},
                    "record_kind": {"type": ["string", "null"], "description": "records_search 的记录类型（note/meeting/card/call/other）；不限填 null"},
                    "location": {"type": ["string", "null"], "description": "records_search 的地点关键词；不限填 null"},
                    "entity_kind": {"type": ["string", "null"], "description": "entity_graph 的中心实体类型（person/org/matter/record）"},
                    "entity_id": {"type": ["string", "null"], "description": "entity_graph 的中心实体标识"},
                    "depth": {"type": ["integer", "null"], "minimum": 0, "maximum": 3, "description": "entity_graph 遍历跳数（1–3，0 或 null 表示默认 2 跳）"},
                },
                ["kind"],
                self._read_personal,
                parallel=True,
            ),
            self._spec("list_artisans", "列出本人此刻可用的匠人（企业业务能力）及其说明、承载方式（是否支持附带文件材料）、连接器与沙盒能力。",
                       {}, [], self._list_artisans, parallel=True),
            self._spec("dispatch_artisan", "把任务交给一位匠人；立即返回，不等结果。",
                       {"package": {"type": "string"}, "entry": {"type": "string"},
                        "instruction": {"type": "string", "description": "给匠人的任务说明"},
                        "materials": {"type": "array", "items": {
                            "type": "object", "properties": {
                                "item_id": {"type": "string"}, "revision": {"type": "integer"}},
                            "required": ["item_id", "revision"]}}},
                       ["package", "entry", "instruction"], self._dispatch),
            self._spec("check_dispatch", "查一次派工的当前状态、验收结果、交付成果摘录与待处理问题。",
                       {"dispatch_id": {"type": "string"}}, ["dispatch_id"],
                       self._check_dispatch, parallel=True),
            *self._c2_specs(),
            *self._group_specs(),
            *self._limit_specs(),
            self._spec(
                "update_task_board",
                "创建或更新当前 trace 的跨轮持久任务板（含 title、实时 focus、status 与支持 blocked_by DAG 的步骤列表），"
                "用于多步任务防跨轮失忆并同步展示给员工。",
                {
                    "title": {"type": "string", "description": "任务板标题（1–160 字）"},
                    "focus": {"type": ["string", "null"], "description": "当前正在推进的一句话焦点"},
                    "status": {"type": "string", "enum": ["active", "completed", "failed", "cancelled"]},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "done", "skipped", "failed"]},
                                "blocked_by": {"type": "array", "items": {"type": "string"}},
                                "ref": {"type": ["string", "null"]},
                            },
                            "required": ["id", "title"],
                        },
                    },
                },
                ["title"],
                self._update_task_board,
            ),
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
        from server.engine.butler.groups.tools import GroupTools

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
        """管家调整自己的时限与这次请求的预算（R6-B、R6-C）。"""
        from server.engine.butler.limits import LimitTools

        handlers = LimitTools(self._platform)
        return [
            self._spec("extend_turn", "这件事你还需要更多时间亲自处理时，说明理由并延长本轮时限"
                       "（从现在起再给 seconds 秒，有上限）。长的工作优先拆成派工或编组。",
                       {"seconds": {"type": "integer", "minimum": 1},
                        "reason": {"type": "string", "description": "为什么还需要这些时间"}},
                       ["seconds", "reason"], handlers.extend_turn),
            self._spec("raise_budget", "这件事需要比起始预算更多的 token、管家处理轮数或匠人时，说明理由"
                       "把这次请求的该项预算提高到 to（只能提高，有上限；企业每日用量配额不变）。",
                       {"budget": {"type": "string", "enum": ["tokens", "butler_turns", "artisans"]},
                        "to": {"type": "integer", "minimum": 1},
                        "reason": {"type": "string", "description": "为什么需要更多"}},
                       ["budget", "to", "reason"], handlers.raise_budget),
        ]

    def _c2_specs(self) -> list[taifeng.ToolSpec]:
        """取消、答复待处理项、代答与转问。"""
        from server.engine.butler.wake_tools import WakeTools

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
                result = await handler(turn, args, ctx.call_id)
                hint = None if result.is_error or name == "extend_turn" else time_hint(turn)
                return replace(result, output=result.output + hint) if hint else result
            except PlatformError as exc:
                return taifeng.ToolResult.error(f"{exc.code}: {exc.message}")
            except ValidationError as exc:
                return taifeng.ToolResult.error(f"invalid_arguments: {_validation_summary(exc)}")
            except ValueError as exc:
                detail = " ".join(str(exc).split())[:240]
                return taifeng.ToolResult.error(f"invalid_arguments: {detail or type(exc).__name__}")

        schema = {"type": "object", "properties": properties, "required": required,
                  "additionalProperties": False}
        return taifeng.ToolSpec(name=name, description=description, input_schema=schema,
                                handler=run, parallel_safe=parallel, timeout_seconds=60.0)

    async def _propose(self, turn: ButlerTurn, args: dict[str, Any], _call: str) -> taifeng.ToolResult:
        """解析内置能力选择，产出与分流相同的待确认卡片；每轮最多一个。"""
        from server.engine.assistant.service import Choice

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

    async def _act_personal(self, turn: ButlerTurn, args: dict[str, Any], _call: str,
                            ) -> taifeng.ToolResult:
        """直接代员工办理本人私域的整理、待办、日程、提醒、事项或文档归档。"""
        result = await execute_personal_action(self._platform, turn, args, now=self._now())
        return taifeng.ToolResult.ok(_json(result))

    async def _read_personal(self, turn: ButlerTurn, args: dict[str, Any], _call: str,
                             ) -> taifeng.ToolResult:
        """本人的今日全景、待办、日程、提醒、文件、知识库、工作与个人知识图谱（只读）。"""
        kind = str(args.get("kind") or "")
        extended = await read_extended_personal(self._platform, turn, kind, args, now=self._now())
        if extended is not None:
            return taifeng.ToolResult.ok(_json(extended))
        if kind == "reminders":
            records = await self._platform.reminders.list(turn.who, status="active")
            items = [{"reminder_id": r.reminder_id, "version": r.version, "title": r.title,
                      "due_at": r.due_at, "repeat": r.repeat, "status": r.status}
                     for r in records[:_LIST_MAX]]
            return taifeng.ToolResult.ok(_json({"items": items}))
        if kind == "work_tasks":
            tasks = await self._platform.work_tasks.store.list(turn.who, limit=_LIST_MAX)
            items = [{"task_id": t.task_id, "package": t.package, "status": t.status}
                     for t in tasks]
            return taifeng.ToolResult.ok(_json({"items": items}))
        if kind == "matters":
            q = args.get("query")
            search = q.strip() if isinstance(q, str) and q.strip() else None
            matters = await self._platform.workspace.list(turn.who, kind="matter", search=search)
            items = [
                {
                    "id": m["id"],
                    "version": m["version"],
                    "title": m["data"].get("title", ""),
                    "status": m["data"].get("status", "active"),
                    "description": str(m["data"].get("description") or "")[:200],
                }
                for m in matters[:_LIST_MAX]
            ]
            return taifeng.ToolResult.ok(_json({"items": items}))
        if kind == "orgs_search":
            q = args.get("query")
            tag = args.get("tag")
            orgs = await self._platform.people.list_orgs(
                turn.who,
                q=q.strip() if isinstance(q, str) and q.strip() else None,
                tag=tag.strip() if isinstance(tag, str) and tag.strip() else None,
                limit=15,
            )
            return taifeng.ToolResult.ok(_json({"items": orgs}))
        if kind == "records_search":
            q = args.get("query")
            rk = args.get("record_kind")
            loc = args.get("location")
            pid = args.get("person_id")
            oid = args.get("org_id")
            mid = args.get("matter_id")
            recs = await self._platform.people.list_notes(
                turn.who,
                q=q.strip() if isinstance(q, str) and q.strip() else None,
                record_kind=rk.strip() if isinstance(rk, str) and rk.strip() else None,
                location=loc.strip() if isinstance(loc, str) and loc.strip() else None,
                person_id=pid.strip() if isinstance(pid, str) and pid.strip() else None,
                org_id=oid.strip() if isinstance(oid, str) and oid.strip() else None,
                matter_id=mid.strip() if isinstance(mid, str) and mid.strip() else None,
                limit=15,
            )
            return taifeng.ToolResult.ok(_json({"items": recs}))
        if kind == "entity_graph":
            ek = args.get("entity_kind")
            eid = args.get("entity_id")
            if not isinstance(ek, str) or not ek.strip() or not isinstance(eid, str) or not eid.strip():
                return taifeng.ToolResult.error("entity_graph 需要提供 entity_kind 与 entity_id")
            d = args.get("depth")
            depth = d if type(d) is int and 1 <= d <= 3 else 2
            graph = await self._platform.people.entity_graph(turn.who, ek.strip(), eid.strip(), depth=depth)
            return taifeng.ToolResult.ok(_json(graph))
        if kind == "people_search":
            q = args.get("query")
            org = args.get("organization")
            tag = args.get("tag")
            bm = args.get("birth_month")
            birth_month = bm if type(bm) is int and 1 <= bm <= 12 else None
            people = await self._platform.people.list_people(
                turn.who,
                q=q.strip() if isinstance(q, str) and q.strip() else None,
                organization=org.strip() if isinstance(org, str) and org.strip() else None,
                tag=tag.strip() if isinstance(tag, str) and tag.strip() else None,
                birth_month=birth_month,
                limit=10,
            )
            return taifeng.ToolResult.ok(_json({"items": people}))
        if kind == "person_detail":
            person_id = args.get("person_id")
            if not isinstance(person_id, str) or not person_id.strip():
                return taifeng.ToolResult.error("person_detail 需要提供 person_id")
            detail = await self._platform.people.get_person(turn.who, person_id)
            graph = await self._platform.people.graph(turn.who, person_id, depth=2)
            return taifeng.ToolResult.ok(_json({**detail, "graph": graph}))
        if kind == "matter_people":
            matter_id = args.get("matter_id")
            if not isinstance(matter_id, str) or not matter_id.strip():
                return taifeng.ToolResult.error("matter_people 需要提供 matter_id")
            items = await self._platform.people.list_matter_people(turn.who, matter_id)
            return taifeng.ToolResult.ok(_json({"items": items}))
        return taifeng.ToolResult.error(
            "不支持的 read_personal kind：" + kind
        )

    async def _list_artisans(self, turn: ButlerTurn, _args: dict[str, Any], _call: str,
                             ) -> taifeng.ToolResult:
        """本人可用的匠人及能力画像（是否接收材料、连接器名与沙盒能力）；说明文字是数据。"""
        artisans = await self._platform.artisans.available(turn.who)
        return taifeng.ToolResult.ok(_json({"artisans": [
            {"package": a.ref.package, "entry": a.ref.entry,
             "name": display_name_for(a.display_name, "zh-CN") or a.ref.package,
             "entry_name": display_name_for(a.entry_display_name, "zh-CN") or a.ref.entry,
             "description": a.description,
             "entry_description": a.entry_description,
             "host": a.host,
             "accepts_materials": a.host == "task",
             "connectors": list(a.connectors),
             "capabilities": {"shell": a.sandbox_shell, "network": a.sandbox_network}}
            for a in artisans]}))

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
        from server.engine.butler.groups.divergence import Rating, valid_rating

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
        """本人派工的状态、验收与交付成果摘录；他人的与不存在的一样不可见。"""
        from server.api.dispatches import dispatch_detail

        owner = turn.owner()
        dispatch_id = str(args["dispatch_id"])
        record = await self._platform.dispatches.get(owner, dispatch_id)
        wake = self._platform.store.wake_store
        events = await wake.events_for_dispatch(owner, dispatch_id)
        items = await wake.list_pending(owner, open_only=False, dispatch_id=dispatch_id)
        detail = dispatch_detail(record, events, items)
        return taifeng.ToolResult.ok(_json({
            **detail,
            "artisan": record.artisan.package,
            "entry": record.artisan.entry,
            "materials": list(record.materials),
        }))

    async def _update_task_board(self, turn: ButlerTurn, args: dict[str, Any], _call: str,
                                 ) -> taifeng.ToolResult:
        """创建或更新当前 trace 的跨轮持久任务板，并推送实时焦点给纯 Web 订阅者。"""
        from datetime import UTC, datetime

        from server.core.ids import new_id
        from server.engine.butler.task_board import TaskBoardRecord, TaskBoardStep, board_view

        owner = turn.owner()
        now = datetime.fromtimestamp(self._now(), UTC)
        raw_steps = args.get("steps") or []
        steps = tuple(TaskBoardStep.model_validate(s) for s in raw_steps)
        record = TaskBoardRecord(
            board_id=new_id("btb"),
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            trace_id=turn.trace_id,
            title=str(args["title"]).strip(),
            focus=str(args["focus"]).strip() if isinstance(args.get("focus"), str) and args["focus"].strip() else None,
            status=args.get("status") or "active",
            steps=steps,
            created_at=now,
            updated_at=now,
        )
        saved = await self._platform.store.task_board_store.upsert(owner, record)
        snapshot = board_view(saved)
        turn.note_focus(snapshot)
        return taifeng.ToolResult.ok(_json(snapshot))
