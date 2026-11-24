"""编组只读接口（R5-D2）：本人编组的进展与会诊消息区。

只给本人：需要当前启用的员工身份，机器身份 403，他人的与不存在的一律 404。只返回成员
发布到消息区的内容与编组元数据，不含匠人的内部过程。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析
from server.engine.tracing.models import TraceOwner

if TYPE_CHECKING:
    from server.core.tenancy import Principal
    from server.engine.butler.groups.messages import GroupMessage
    from server.engine.butler.groups.plan import GroupRecord
    from server.platform import Platform


def group_detail(record: GroupRecord) -> dict[str, Any]:
    """编组进展：计划结构、成员与追问结果、各步裁决；不回显租户与员工标识。"""
    return {
        "group_id": record.group_id, "trace_id": record.trace_id, "title": record.plan.title,
        "state": record.state, "step": record.step, "summarized": record.summarized,
        "cancel_requested": record.cancel_requested, "version": record.version,
        "steps": [{"mode": s.mode, "members": [m.package for m in s.members]}
                  for s in record.plan.steps],
        "members": [{"step": r.step, "member": r.index + 1, "dispatch_id": r.dispatch_id,
                     "outcome": r.outcome, "end_reason": r.end_reason} for r in record.runs],
        "followups": [{"step": f.step, "member": f.target + 1, "asked_by": f.asked_by,
                       "round": f.round, "dispatch_id": f.dispatch_id, "outcome": f.outcome}
                      for f in record.followups],
        "verdicts": [{"step": v.step, "level": v.level, "reasons": list(v.reasons),
                      "rated_level": v.rated_level, "pending_id": v.pending_id,
                      "ruled": v.ruling is not None} for v in record.verdicts],
        "created_at": record.created_at.isoformat(), "updated_at": record.updated_at.isoformat(),
    }


def message_view(message: GroupMessage) -> dict[str, Any]:
    """消息区的一条：作者、轮次、@ 到的成员（从 1 起）、内容。"""
    return {
        "seq": message.seq, "step": message.step, "author": message.author,
        "member": None if message.member is None else message.member + 1,
        "round": message.round, "mentions": [i + 1 for i in message.mentions],
        "text": message.text, "created_at": message.created_at.isoformat(),
    }


def groups_router(platform: Platform) -> APIRouter:
    """注册编组只读接口。"""
    router = APIRouter()

    async def owner(who: Principal) -> TraceOwner:
        """只有当前启用的员工能看自己的编组。"""
        tenant_id, user_id = await platform.workspace.require_employee(who)
        return TraceOwner(tenant_id=tenant_id, user_id=user_id)

    @router.get("/v1/assistant/groups/{group_id}")
    async def detail(group_id: str, who: Caller) -> dict[str, Any]:
        """一个编组的进展。"""
        return group_detail(await platform.butler.groups.get(await owner(who), group_id))

    @router.get("/v1/assistant/groups/{group_id}/messages")
    async def messages(group_id: str, who: Caller) -> dict[str, Any]:
        """会诊消息区，按序号；编组不属于本人时 404。"""
        me = await owner(who)
        await platform.butler.groups.get(me, group_id)
        found = await platform.store.group_message_store.list(me, group_id)
        return {"items": [message_view(m) for m in found]}

    return router
