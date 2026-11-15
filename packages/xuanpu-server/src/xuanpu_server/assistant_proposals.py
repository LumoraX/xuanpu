"""平台内置能力的提案解析：把模型给出的选择变成待确认草稿或原生入口。

由直接对话分流（``AssistantRouter``）与管家（``butler``）共用：两边产出完全相同的待确认
卡片，任何写入仍须员工本人确认。本模块不选择业务包、不建会话；不属于内置能力的选择
返回 None，由调用方处理（分流去选业务，管家去派工或直接回答）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.assistant_reminders import REMINDER_PACKAGE, RouteOutcome
from xuanpu_server.assistant_task_definitions import resolve_definition
from xuanpu_server.assistant_workspace import MODULES, WORKSPACE_PACKAGE
from xuanpu_server.collaboration.routing import resolve as resolve_collaboration
from xuanpu_server.errors import RuntimeUnavailableError
from xuanpu_server.proactive.models import ActionInput, absolute_time
from xuanpu_server.shared_matters.routing import resolve_shared

if TYPE_CHECKING:
    from xuanpu_server.assistant import Choice
    from xuanpu_server.platform import Platform
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.work_tasks.models import InputFile

NATIVE_PACKAGES = frozenset({
    "@collaboration", "@shared", "@task-definitions", "@connections", "@proactive",
    WORKSPACE_PACKAGE, REMINDER_PACKAGE,
})


async def resolve_native_choice(
    platform: Platform, who: Principal, choice: Choice, *, now: float,
    matter_id: str | None = None, request_id: str | None = None,
    input_files: list[InputFile] | tuple[InputFile, ...] = (),
) -> RouteOutcome | None:
    """解析内置能力的选择；不是内置能力时返回 None。

    校验规则与原分流一致：草稿不能夹带别的操作，非对应能力不能携带对应草稿。

    Raises:
        ValueError: 选择不完整或夹带了不相干的草稿。
    """
    collaboration = await _collaboration_shared_definitions(
        platform, who, choice, matter_id, request_id, input_files)
    if collaboration is not None:
        return collaboration
    if choice.package == "@proactive":
        return await _proactive(platform, who, choice)
    if choice.proactive is not None:
        raise ValueError("其他业务不能携带跟进动作")
    if choice.package == WORKSPACE_PACKAGE:
        return await _workspace(platform, who, choice, matter_id)
    if choice.workspace_draft is not None:
        raise ValueError("非工作站操作不得附带写入建议")
    if choice.package == REMINDER_PACKAGE and who.user_id and choice.message is None:
        return _reminder(choice, now)
    if choice.reminder is not None:
        raise ValueError("非提醒业务不能携带提醒建议")
    return None


async def _collaboration_shared_definitions(
    platform: Platform, who: Principal, choice: Choice, matter_id: str | None,
    request_id: str | None, input_files: list[InputFile] | tuple[InputFile, ...],
) -> RouteOutcome | None:
    """平台协作只提出草稿或原生入口，不从模型文字执行权威状态操作。"""
    if choice.package == "@collaboration":
        if choice.message is not None or any((choice.reminder, choice.workspace_draft,
                                              choice.proactive, choice.task_definition,
                                              choice.shared_draft)):
            raise ValueError("交接草稿不能夹带其他操作")
        return await resolve_collaboration(platform, who, choice.entry, choice.handoff_draft)
    if choice.package == "@shared":
        if choice.message is not None or any((choice.reminder, choice.workspace_draft,
                                              choice.proactive, choice.task_definition)):
            raise ValueError("共享建议不能夹带其他操作")
        return await resolve_shared(platform, who, choice.entry, choice.shared_draft)
    if choice.package == "@task-definitions":
        return await resolve_definition(platform, who, choice.entry, choice.task_definition,
                                        input_files, matter_id, request_id)
    if (choice.package == "@connections" and who.user_id
            and platform.connections.config.allows(who.require_tenant())):
        return RouteOutcome(message="请在本人连接页查看应用范围并确认；也可以直接撤销已有授权。",
                            open_connections=True)
    return None


async def _proactive(platform: Platform, who: Principal, choice: Choice) -> RouteOutcome:
    """业务跟进：打开列表，或对一条跟进提出处理草稿（版本须一致）。"""
    if not who.user_id or choice.message is not None or choice.reminder or choice.workspace_draft:
        raise ValueError("业务跟进建议无效")
    if choice.entry == "manage" and choice.proactive is None:
        return RouteOutcome(message="已打开你的业务跟进。", open_proactive=True)
    if choice.entry != "action" or choice.proactive is None:
        raise ValueError("业务跟进建议不完整")
    suggestion = choice.proactive
    record = await platform.proactive.get(who, suggestion.id)
    if record["version"] != suggestion.version:
        raise RuntimeUnavailableError("业务跟进已更新，请重试")
    action = ActionInput(request_id="preview", version=suggestion.version,
                         action=suggestion.action, due_at=absolute_time(suggestion.due_at),
                         timezone=suggestion.timezone)
    draft = {**action.model_dump(exclude={"request_id"}), "id": record["id"],
             "item_version": record.get("item_version"), "title": record["title"]}
    return RouteOutcome(message="请确认这项业务跟进的处理方式。", proactive_draft=draft)


async def _workspace(
    platform: Platform, who: Principal, choice: Choice, matter_id: str | None,
) -> RouteOutcome:
    """个人工作站：打开模块，或提出新建草稿（关联记录须是本人有效记录）。"""
    if not who.user_id or choice.message is not None or choice.reminder is not None:
        raise ValueError("工作站建议无效")
    if choice.entry in MODULES and choice.workspace_draft is None:
        return RouteOutcome(message="已打开你的" + MODULES[choice.entry] + "。",
                            open_workspace=choice.entry)
    if choice.entry != "create" or choice.workspace_draft is None:
        raise ValueError("工作站建议不完整")
    draft = choice.workspace_draft.preview()
    if matter_id and draft["kind"] in {"todo", "event", "file"} and not draft["data"].get("matter_id"):
        draft["data"]["matter_id"] = matter_id
    for field in ("parent_id", "matter_id"):
        if draft["data"].get(field):
            record = await platform.workspace.get(who, draft["data"][field])
            expected = "folder" if field == "parent_id" else "matter"
            if record["kind"] != expected or record["trashed_at"] is not None:
                raise ValueError("关联记录无效")
    return RouteOutcome(message="已整理好，请确认后保存到玄圃。", workspace_draft=draft)


def _reminder(choice: Choice, now: float) -> RouteOutcome:
    """个人提醒：打开列表，或提出提醒草稿（时间不明确时追问）。"""
    if choice.entry == "manage" and choice.reminder is None:
        return RouteOutcome(message="可以在提醒列表中查看、修改或取消。", open_reminders=True)
    if choice.entry == "create" and choice.reminder is not None:
        try:
            draft = choice.reminder.preview(now)
        except (ValueError, KeyError):
            return RouteOutcome(
                message="这个提醒时间不明确或已经过去，请告诉我准确的日期、时间和时区。")
        return RouteOutcome(message="请确认下面的提醒时间和内容。", reminder_draft=draft)
    raise ValueError("提醒建议不完整")
