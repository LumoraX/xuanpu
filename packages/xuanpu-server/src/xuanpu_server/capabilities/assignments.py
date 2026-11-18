"""到人能力分配（R4-B0）：管理员把能力的明确修订分配给员工，或可选地分配给岗位/部门。

- 直接到人是默认，不需要部门或岗位；按岗位/部门须引用真实结构及其当前版本。
- 分配只授权使用能力，不授予读文件、使用连接或组织审批（那些仍由各自的权限决定）。
- 所有写操作带 ``request_id`` 幂等（同键同体回原回执，不同体 409），撤销带 ``expected_version``；
  撤销把代次加一，下一次调用立即被拒（引擎回收由既有维护循环完成，不等异步清理）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, InvalidRequestError, NotFoundError
from xuanpu_server.ids import new_id

from .models import CapabilityAssignment
from .resolver import resolvable

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.tenancy import Principal

    from .schemas import AssignInput, RevokeInput
    from .service import CapabilityService

_MAX_ASSIGNMENTS = 10_000


def assignment_view(record: CapabilityAssignment) -> dict[str, JsonValue]:
    """管理视图：目标、明确修订范围、期限与状态；不推导任何资料权限。"""
    return {'id': record.id, 'capability_id': record.capability_id, 'version': record.version,
            'selector': record.selector.model_dump(mode='json'),
            'allowed_revisions': list(record.allowed_revisions), 'valid_until': record.valid_until,
            'status': record.status, 'epoch': record.epoch, 'created_at': record.created_at,
            'grants': 'capability_use_only'}


class CapabilityAssignments:
    """分配的增、撤、查；与能力登记共用协调事务与回执。"""

    def __init__(self, service: CapabilityService) -> None:
        """复用能力服务的管理员复核、时钟与仓储。"""
        self.service = service

    async def assign(self, who: Principal, capability_id: str, body: AssignInput) -> dict[str, JsonValue]:
        """校验能力、修订与目标都真实存在后登记；目标代次或结构版本不符 409。"""
        s = self.service
        who = await s.admin(who, mutation=True)
        tenant = who.require_tenant()
        payload = {'kind': 'capability.assign', 'capability_id': capability_id, **body.model_dump(mode='json')}
        target_user = None
        if body.selector.kind == 'user':
            target_user = await s.organization.identity.get_user(tenant, body.selector.id)
        async with s.store.transaction(tenant, participants=s.participants(who)) as tx:
            await s.admin(who, mutation=True)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            state = tx.state.capabilities
            numbers = {r.revision for r in state.revisions.values() if r.capability_id == capability_id}
            if not numbers:
                raise NotFoundError('能力不存在')
            if not set(body.allowed_revisions) <= numbers:
                raise InvalidRequestError('allowed_revisions 含不存在的能力修订')
            self._target(tx, body, target_user)
            if len(state.assignments) >= _MAX_ASSIGNMENTS:
                raise ConflictError('能力分配达到上限')
            record = CapabilityAssignment(
                tenant_id=tenant, id=new_id('cas'), created_at=s.now(), capability_id=capability_id,
                selector=body.selector, allowed_revisions=tuple(body.allowed_revisions),
                valid_until=body.valid_until)
            state.assignments[record.id] = record
            tx.emit('capability.assigned', record.id, record.version, actor=who.actor, at=s.now())
            result = assignment_view(record)
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    @staticmethod
    def _target(tx: CoordinationTransaction, body: AssignInput, user: object) -> None:
        """目标须真实存在且代次/版本与确认时一致；客户端不能自报任职。"""
        selector = body.selector
        if selector.kind == 'user':
            if (user is None or getattr(user, 'status', None) != 'active'
                    or getattr(user, 'lifecycle_epoch', None) != selector.user_epoch):
                raise ConflictError('员工不存在、已停用或代次已变化，请刷新后重新确认')
            return
        records = tx.organization.positions if selector.kind == 'position' else tx.organization.units
        target = records.get(selector.id)
        if target is None or target.status != 'active':
            raise NotFoundError('岗位或部门不存在')
        if target.version != selector.target_version:
            raise ConflictError('岗位或部门已变化，请刷新后重新确认')

    async def revoke(self, who: Principal, assignment_id: str, body: RevokeInput) -> dict[str, JsonValue]:
        """按版本撤销：代次加一，下一次调用即被拒；已撤销的不能复活。"""
        s = self.service
        who = await s.admin(who)
        payload = {'kind': 'capability.revoke', 'assignment_id': assignment_id, **body.model_dump(mode='json')}
        async with s.store.transaction(who.require_tenant(), participants=s.participants(who)) as tx:
            await s.admin(who)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            item = tx.state.capabilities.assignments.get(assignment_id)
            if item is None:
                raise NotFoundError('能力分配不存在')
            if item.version != body.expected_version:
                raise ConflictError('能力分配已变化，请刷新后重试')
            if item.status == 'revoked':
                raise ConflictError('能力分配已撤销')
            changed = item.model_copy(update={'status': 'revoked', 'epoch': item.epoch + 1,
                                              'version': item.version + 1})
            tx.state.capabilities.assignments[assignment_id] = changed
            tx.emit('capability.assignment_revoked', assignment_id, changed.version, actor=who.actor, at=s.now())
            result = assignment_view(changed)
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    async def listing(self, who: Principal, capability_id: str) -> dict[str, JsonValue]:
        """某能力的全部分配（含已撤销），仅管理员。"""
        s = self.service
        who = await s.admin(who)
        async with s.store.transaction(who.require_tenant(), participants=s.participants(who)) as tx:
            await s.admin(who)
            items = sorted((a for a in tx.state.capabilities.assignments.values()
                            if a.capability_id == capability_id), key=lambda a: (a.created_at, a.id))
            return {'assignments': [assignment_view(a) for a in items], 'total': len(items)}

    async def mine(self, who: Principal) -> dict[str, JsonValue]:
        """本人此刻可新受理的能力；只给说明与依据，不暴露其他人的分配。"""
        s = self.service
        who = await s.organization.actor(who, employee=True)
        tenant = who.require_tenant()
        assert who.user_id is not None
        user = await s.organization.identity.get_user(tenant, who.user_id)
        if user is None:
            raise NotFoundError('员工不存在')
        async with s.store.transaction(tenant, participants=s.participants(who)) as tx:
            await s.organization.actor(who, employee=True)
            decisions = resolvable(tx.state.capabilities, tx.organization, user_id=who.user_id,
                                   user_epoch=user.lifecycle_epoch, now=s.now())
            revisions = {(r.capability_id, r.revision): r for r in tx.state.capabilities.revisions.values()}
        capabilities: list[JsonValue] = []
        for d in decisions:
            delivery = revisions[(d.capability_id, d.capability_revision)].delivery
            capabilities.append({
                'capability_id': d.capability_id, 'capability_key': d.capability_key,
                'revision': d.capability_revision, 'title': delivery.title,
                'summary': delivery.summary, 'package': d.package_name, 'entry': d.entry,
                'assignment_id': d.assignment_id, 'can_run': True})
        if not capabilities:
            return {'capabilities': [], 'total': 0, 'status': 'not_assigned', 'message': '尚未分配可运行能力'}
        return {'capabilities': capabilities, 'total': len(capabilities), 'status': 'assigned'}
