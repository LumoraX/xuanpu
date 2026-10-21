"""开发后端的已提交触发候选索引等价语义。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xuanpu_server.workspace.models import WorkspaceState


def candidates(states: dict[tuple[str,str],WorkspaceState],kind: str, *, after: tuple[str,str,str] | None,limit: int,now: float) -> list[tuple[str,str,str]]:
    """扫描不加载对象正文，不在没有资格时循环消耗attempt。"""
    if kind not in {'time','event'} or not 1<=limit<=100:
        raise ValueError('触发候选参数无效')
    found=[]
    for (tenant,user),state in sorted(states.items()):
        records=state.task_definitions if kind=='time' else state.task_event_deliveries
        for key,value in sorted(records.items()):
            identity=tenant,user,key
            if after is not None and identity<=after:
                continue
            if kind=='time':
                due=value['next_eligibility_check_at'] or value['next_fire_at']
                eligible=value['enabled'] and due is not None and due<=now
            else:
                eligible=value['status']!='settled' and value['lease_until']<=now and value['next_check_at']<=now
            if eligible:
                found.append(identity)
            if len(found)==limit:
                return found
    return found
