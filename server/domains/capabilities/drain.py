"""首次纳管的排空预览（R4-C1）：登记能力前先看这个包还有多少没有能力依据的在途工作。

一个包的任一入口登记为能力后整包纳管；纳管前开始的会话与任务没有调用绑定，续跑时会被拒。
预览只读、只给管理员：数出这个包仍在进行（``active``）的会话里没有绑定的那些（任务承载的会话
对照任务绑定），不含正文、标题或员工明细；定时安排等后台安排不在这里统计。
扫描有上界，超过上界如实标 ``truncated``，不把截断后的数当全部。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from server.domains.capabilities.resolver import managed_package

if TYPE_CHECKING:
    from pydantic import JsonValue

    from server.core.tenancy import Principal
    from server.domains.capabilities.service import CapabilityService

SCAN_LIMIT = 2000


async def drain_preview(service: CapabilityService, who: Principal, package: str) -> dict[str, JsonValue]:
    """这个包没有能力依据的在途会话与任务承载会话数；已纳管时同样给出（续跑将被拒的数量）。"""
    who = await service.admin(who)
    tenant = who.require_tenant()
    state, _ = await service.store.capability_snapshot(tenant)
    control = service.organization.control
    sessions = await control.list_sessions(tenant, status='active', limit=SCAN_LIMIT)
    bindings = control.binding_store
    conversations = tasks = 0
    for record in sessions:
        if record.package != package:
            continue
        if record.work_task_id is not None:
            if await bindings.get(tenant, 'task', record.work_task_id) is None:
                tasks += 1
        elif await bindings.get(tenant, 'session', record.session_id) is None:
            conversations += 1
    unbound = conversations + tasks
    return {'package': package, 'managed': managed_package(state, package),
            'unbound_sessions': conversations, 'unbound_task_sessions': tasks,
            'truncated': len(sessions) >= SCAN_LIMIT, 'scheduled_not_counted': True,
            'advice': ('没有待排空的在途工作' if not unbound else
                       '纳管后这些工作续跑会被拒：请先让员工结束或等待完成，再登记能力')}
