"""普通助手只提出交接说明草稿或打开入口，不从文字执行受理和审批。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from xuanpu_server.assistant_reminders import RouteOutcome
from xuanpu_server.errors import InvalidRequestError, PermissionDeniedError

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform
    from xuanpu_server.tenancy import Principal

COLLABORATION_ENTRIES=[{'package':'@collaboration','version':'1','entry':entry,
    'description':'玄圃到人交接与组织审批，无需部门；由当前员工原生确认',
    'entry_description':description} for entry,description in (
        ('manage','查看本人收到、发出、承办和审批事项'),
        ('draft','提出交接名称与说明草稿，员工再明确来源、接收人及范围'))]
COLLABORATION_PROMPT='''\n交接与组织审批选择@collaboration/manage；提出交接说明选择@collaboration/draft，
message=null并加handoff_draft:{title,summary}。草稿不能包含员工、部门、来源、材料、角色、动作或审批人。
接受、转交、审批、交付、签收必须原生确认，不声称已经执行。共享事实必须先在本人当前资料范围内明确选择后阅读。
'''


class HandoffProposal(BaseModel):
    """模型草稿不接受目标身份、来源范围和任何权威业务状态。"""
    model_config=ConfigDict(extra='forbid',strict=True)
    title: str=Field(min_length=1,max_length=500,pattern=r'\S')
    summary: str=Field(min_length=1,max_length=4000,pattern=r'\S')


async def resolve(platform: Platform,who: Principal,entry: str|None,draft: HandoffProposal|None) -> RouteOutcome:
    """草稿不产生交接记录；新业务开关关闭时仍允许本人查看既有工作。"""
    await platform.handoffs.shared.actor(who)
    if entry=='manage' and draft is None:
        return RouteOutcome(message='请查看本人收到、发出、承办或需要审批的工作。',open_collaboration=True)
    if entry!='draft' or draft is None:
        raise InvalidRequestError('交接说明草稿不完整')
    if not platform.handoffs.config.allows(who.require_tenant()):
        raise PermissionDeniedError('新的工作交接尚未启用')
    return RouteOutcome(message='请核对交接说明，再明确共享来源、接收员工和资料范围。',
        handoff_draft=draft.model_dump(),open_collaboration=True)
