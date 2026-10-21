"""主对话把办公交办受理为持久任务，不能把模型选择当作执行结果。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.assistant_reminders import RouteOutcome
from xuanpu_server.errors import NotFoundError

from .models import WorkTaskInput
from .service import OFFICE_PACKAGE

if TYPE_CHECKING:
    from xuanpu_server.assistant import RouteBody
    from xuanpu_server.platform import Platform
    from xuanpu_server.tenancy import Principal


async def accept_office_route(platform: Platform, who: Principal, body: RouteBody,
                              entry: str | None, request_id: str | None) -> RouteOutcome:
    """服务器核对实际包和输入版本，再返回已提交的task_id。"""
    try:
        package = await platform.packages.resolve(who.require_tenant(), OFFICE_PACKAGE)
    except NotFoundError:
        return RouteOutcome(message='企业尚未开通办公材料处理，原文和附件已保留。', preserve_input=True)
    if entry not in package.manifest.spec.skills.entries:
        raise NotFoundError('该办公能力入口未开通')
    if (not who.user_id or not request_id or not body.input_files
        or {entry.item_id for entry in body.input_files} != set(body.attachments)):
        return RouteOutcome(message='请重新选择本人文件的明确版本，再交办这项工作。', preserve_input=True)
    task = await platform.work_tasks.accept(who, WorkTaskInput(request_id='office:' + request_id,
        text=body.text, input_files=tuple(body.input_files), timezone=body.timezone, matter_id=body.matter_id))
    return RouteOutcome(message='工作已受理。你可以离开页面，回来后查看进展和成果。', work_task_id=task.task_id)
