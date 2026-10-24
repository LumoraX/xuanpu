"""模型只提出共享草稿或打开入口，成员与权限由员工原生确认。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from xuanpu_server.assistant_reminders import RouteOutcome
from xuanpu_server.errors import InvalidRequestError

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform
    from xuanpu_server.tenancy import Principal

SHARED_ENTRIES = [{'package': '@shared', 'version': '1', 'entry': entry,
    'description': '玄圃共享协作，具体员工即可参与，无需部门', 'entry_description': description}
    for entry, description in [('manage', '打开当前可见共享事项和资料，原生选择后阅读'),
        ('create', '提出共享事项名称和正文草稿，待本人原生确认，不自行邀请或授予权限')]]
SHARED_PROMPT = '''\n共享协作直接到具体员工，不要求先建部门。查看共享事项选择@shared/manage。
新建共享事项选择@shared/create，message=null，加shared_draft:{title,content}，只提出草稿。
禁止在草稿中指定成员、权限、负责人、租户或任何身份。邀请、交接、审批都必须另外原生明确确认。
共享正文不在当前普通对话上下文，不能声称已经查看；请打开共享入口明确选择资料再阅读。
'''


class SharedProposal(BaseModel):
    """普通个人对话只可提出独立事项，不夹带授权和业务执行。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    title: str = Field(min_length=1, max_length=500, pattern=r'\S')
    content: str = Field(default='', max_length=20000)


async def resolve_shared(platform: Platform, who: Principal, entry: str | None,
                         draft: SharedProposal | None) -> RouteOutcome:
    """平台当前开关决定新建，查看入口始终走本人当前ACL。"""
    await platform.shared_matters.actor(who)
    if entry == 'manage' and draft is None:
        return RouteOutcome(message='请在共享事项中选择要查看的资料。', open_shared=True)
    if entry != 'create' or draft is None:
        raise InvalidRequestError('共享事项建议不完整，请重新说明')
    platform.shared_matters.enabled(who.require_tenant())
    return RouteOutcome(message='请核对共享事项草稿。确认后，再明确邀请具体员工。',
        shared_draft=draft.model_dump())
