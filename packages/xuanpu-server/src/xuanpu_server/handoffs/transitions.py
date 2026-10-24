"""交接业务边与人员资格分离；本表从不自行认领或签收。"""
from __future__ import annotations

from xuanpu_server.errors import ConflictError

EDGES = {
    ('submitted', 'accept'): 'accepted', ('submitted', 'return'): 'returned',
    ('submitted', 'cancel'): 'cancelled', ('accepted', 'start'): 'in_progress',
    ('accepted', 'request_information'): 'needs_information',
    ('in_progress', 'request_information'): 'needs_information',
    ('needs_information', 'provide_information'): 'submitted',
    ('in_progress', 'deliver'): 'delivered', ('delivered', 'sign_off'): 'signed_off',
    ('delivered', 'dispute'): 'disputed', ('disputed', 'reopen'): 'in_progress',
    ('disputed','transfer'): 'submitted',
}
for _state in ('accepted', 'in_progress', 'needs_information'):
    EDGES[(_state, 'request_cancel')] = _state
    EDGES[(_state, 'confirm_cancel')] = 'cancelled'
    EDGES[(_state, 'transfer')] = 'submitted'


def next_status(state: str, action: str) -> str:
    """非法边和终态统一冲突，幂等回放必须在调用本函数之前完成。"""
    target = EDGES.get((state, action))
    if target is None:
        raise ConflictError('当前交接状态不能执行此操作')
    return target
