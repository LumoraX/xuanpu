"""全企业管理员配置审批规则，业务承办不能给自己创建审批授权。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.approvals.models import ApprovalPolicy, PolicyRevision
from xuanpu_server.approvals.resolver import freeze
from xuanpu_server.approvals.schemas import ChangePolicy
from xuanpu_server.errors import ConflictError, NotFoundError, PermissionDeniedError
from xuanpu_server.ids import new_id
from xuanpu_server.organization.permissions import manages_all
from xuanpu_server.shared_matters.views import page

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.approvals.schemas import PolicyInput
    from xuanpu_server.approvals.service import ApprovalService
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.tenancy import Principal


def require_admin(tx: CoordinationTransaction, who: Principal) -> None:
    """首版只开放企业级管理人，部门经理不能越界配置指定员工规则。"""
    if not manages_all(tx, who):
        raise PermissionDeniedError('需要企业级审批策略管理权限')


async def listing(service: ApprovalService, who: Principal, *, identifier: str | None = None,
                  limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
    """规则配置不返回业务正文；调用时重新检查当前管理资格。"""
    who = await service.handoffs.shared.organization.actor(who)
    async with service.handoffs.store.transaction(who.require_tenant(), participants=(who.user_id,) if who.user_id else ()) as tx:
        who = await service.handoffs.shared.organization.actor(who)
        require_admin(tx, who)
        if identifier is not None:
            policy = tx.collaboration.policies.get(identifier)
            if policy is None:
                raise NotFoundError('审批策略不存在')
            return policy.model_dump(mode='json')
        return page([p.model_dump(mode='json') for p in tx.collaboration.policies.values()], limit=limit, cursor=cursor)


async def write(service: ApprovalService, who: Principal, body: PolicyInput, identifier: str | None = None) -> dict[str, JsonValue]:
    """追加修订与回执原子提交，旧修订和已发生的审批决定保持固定。"""
    who = await service.handoffs.shared.organization.actor(who)
    async with service.handoffs.store.transaction(who.require_tenant(), participants=(who.user_id,) if who.user_id else ()) as read:
        require_admin(read, who)
        users = {s.user_id for s in body.steps if s.user_id is not None} | ({who.user_id} if who.user_id else set())
    payload: dict[str, JsonValue] = {'action':'approval-policy.write','id':identifier,'body':body.model_dump(mode='json')}
    async with service.handoffs.store.transaction(who.require_tenant(), participants=tuple(sorted(users))) as tx:
        who = await service.handoffs.shared.organization.actor(who)
        require_admin(tx, who)
        old = tx.receipt(who.actor, body.request_id, payload)
        if old is not None:
            return old
        if not service.config.allows(tx.tenant_id):
            raise PermissionDeniedError('新审批策略尚未启用')
        current = tx.collaboration.policies.get(identifier or '')
        if identifier is not None:
            if current is None:
                raise NotFoundError('审批策略不存在')
            if not isinstance(body, ChangePolicy) or current.version != body.expected_version:
                raise ConflictError('审批策略已修改，请重新核对')
        now, version = service.handoffs.now(), current.version + 1 if current is not None else 1
        revision = PolicyRevision(version=version, name=body.name, steps=freeze(tx, body.steps))
        policy = ApprovalPolicy(tenant_id=tx.tenant_id,id=identifier or new_id('apl'),version=version,name=body.name,
            status=body.status if isinstance(body, ChangePolicy) else 'active',
            revisions=(*current.revisions,revision) if current is not None else (revision,),
            created_at=current.created_at if current is not None else now,updated_at=now)
        tx.collaboration.policies[policy.id] = policy
        tx.emit('approval_policy.changed',policy.id,version,actor=who.actor,at=now,data={'policy_id':policy.id})
        result = policy.model_dump(mode='json')
        tx.record_receipt(who.actor,body.request_id,payload,result)
        return result


async def available(service: ApprovalService,who: Principal,*,limit: int=50,cursor: str|None=None) -> dict[str,JsonValue]:
    """员工只读可选规则名称与版本，不取得管理权或规则中其他员工的身份信息。"""
    who=await service.handoffs.shared.actor(who)
    assert who.user_id is not None
    async with service.handoffs.store.transaction(who.require_tenant(),participants=(who.user_id,)) as tx:
        await service.handoffs.shared.actor(who)
        return page([{'id':p.id,'version':p.version,'name':p.name} for p in tx.collaboration.policies.values()
            if p.status=='active'],limit=limit,cursor=cursor)
