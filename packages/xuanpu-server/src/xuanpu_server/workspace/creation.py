"""原生创建的事务内主体，交互创建和办公批量确认共用同一领域不变量。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import ConflictError
from xuanpu_server.workspace.rules import audit, check_relations, new_item, public, remember, replay

if TYPE_CHECKING:
    from xuanpu_server.workspace.models import WorkspaceState


def create_in_state(state: WorkspaceState, actor: str, kind: str, checked: dict[str, Any],
                    request_id: str, now: float, proposal_id: str | None = None) -> dict[str, Any]:
    """调用者已完成身份和字段验证，员工事务内原子创建与保存建议回执。"""
    payload = {"op": "create", "kind": kind, "data": checked}
    decision_key = 'proposal:' + proposal_id if proposal_id is not None else None
    if decision_key is not None:
        decision = {"action": "confirm", "data": {"kind": kind, "data": checked}}
        if replay(state, decision_key, decision) is None:
            remember(state, decision_key, decision, {})
            audit(state, actor, "proposal_reserved", now, {})
    old = replay(state, request_id, payload)
    if old is not None:
        if decision_key is not None:
            remember(state, decision_key, decision, {"state": "confirmed", "result": old})
        return old
    if len(state.items) >= 10000:
        raise ConflictError("每人最多保存10000条工作台记录")
    item = new_item(kind, checked, now)
    check_relations(state, item)
    state.items[item["id"]] = item
    result = public(item)
    remember(state, request_id, payload, result)
    if decision_key is not None:
        remember(state, decision_key, decision, {"state": "confirmed", "result": result})
    audit(state, actor, "created", now, item)
    return result
