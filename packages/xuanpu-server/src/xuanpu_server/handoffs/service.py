"""到人优先的交接受理，人员、共享资料、责任和回执共用协调事务。"""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from xuanpu_server.collaboration.models import CollaborationConfig
from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
)
from xuanpu_server.handoffs.events import record_event
from xuanpu_server.handoffs.models import Handoff
from xuanpu_server.handoffs.permissions import (
    assignee_current,
    can_view,
    eligible,
    owner_source,
    participant_ids,
    potential_operators,
    source_valid,
)
from xuanpu_server.ids import new_id
from xuanpu_server.shared_matters.permissions import access
from xuanpu_server.shared_matters.views import page

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pydantic import JsonValue

    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.handoffs.schemas import SubmitHandoff
    from xuanpu_server.shared_matters.materials import SharedMaterials
    from xuanpu_server.tenancy import Principal


class HandoffService:
    """关闭新受理仍可查询及完成既有合法动作，不自动猜下一名承办人。"""
    def __init__(self, materials: SharedMaterials, *, config: CollaborationConfig | None = None) -> None:
        """使用共享资料及组织服务的当前身份，不建立第二业务存储或模型worker。"""
        self.materials, self.shared = materials, materials.service
        self.store, self.now = self.shared.store, self.shared.now
        self.config = config or CollaborationConfig()

    async def create(self, who: Principal, body: SubmitHandoff) -> dict[str, JsonValue]:
        """来源owner明确确认目标及资料，在固定锁集内创建唯一交接。"""
        who = await self.shared.actor(who)
        assert who.user_id is not None
        async with self.store.transaction(who.require_tenant(), participants=(who.user_id,)) as read:
            source, _ = access(read, who, body.shared_id, self.now(), owner=True)
            candidates = {who.user_id, *(() if body.target_kind == 'unit' else (body.target_id,))}
            if body.target_kind == 'unit':
                candidates.update(potential_operators(read, body.target_id, self.now()))
        payload: dict[str, JsonValue] = {'action': 'handoff.submit', 'body': body.model_dump(mode='json')}
        async with self.store.transaction(who.require_tenant(), participants=tuple(sorted(candidates)),
                allow_inactive=tuple(sorted(candidates - {who.user_id})) if body.target_kind == 'unit' else ()) as tx:
            await self.shared.actor(who)
            access(tx, who, body.shared_id, self.now(), owner=True)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            if not self.config.allows(tx.tenant_id):
                raise PermissionDeniedError('新的工作交接尚未启用')
            source = await self.shared.writable(tx, who, body.shared_id, body.expected_version, owner=True)
            if source.status != 'active':
                raise ConflictError('请从当前启用的共享事项提交新的工作交接')
            active = sum(h.status not in ('signed_off', 'cancelled', 'returned') for h in tx.collaboration.handoffs.values())
            if active >= self.config.max_active:
                raise ConflictError('当前未结束的交接达到上限')
            self._validate_target(tx, who.user_id, body)
            source_grant = owner_source(tx, source.id, who.user_id)
            revision_ids = self.selected_revisions(tx, who, source.id, body.material_revision_ids)
            now, identifier = self.now(), new_id('hnd')
            policy_version = self.policy_version(tx, body.policy_id)
            handoff = Handoff(tenant_id=tx.tenant_id, id=identifier, shared_id=source.id,
                requester_id=who.user_id, requester_epoch=tx.participant_epochs[who.user_id],
                target_kind=body.target_kind, target_id=body.target_id,
                target_epoch=tx.participant_epochs.get(body.target_id) if body.target_kind == 'user' else None,
                title=body.title, summary=body.summary, content_snapshot=source.content if body.include_content else None,
                material_revision_ids=revision_ids, source_access_epoch=source.access_epoch,
                source_grant_id=source_grant.id, source_grant_epoch=source_grant.epoch,
                input_digest='', due_at=body.due_at, policy_id=body.policy_id, policy_version=policy_version,
                created_at=now, updated_at=now)
            recipients = self.receivers(tx, handoff)
            if not recipients:
                raise InvalidRequestError('目标当前没有可明确受理的员工')
            handoff = handoff.model_copy(update={'input_digest': self.digest(handoff)})
            tx.collaboration.handoffs[identifier] = handoff
            record_event(tx, who, handoff, 'submit', '', now, recipients=recipients)
            result = self.receipt(handoff)
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    def _validate_target(self, tx: CoordinationTransaction, requester: str, body: SubmitHandoff) -> None:
        """首版到人交接需要不同的当前员工，期限必须在未来。"""
        if body.target_kind == 'user' and body.target_id == requester:
            raise InvalidRequestError('请明确另一位接收员工')
        if body.target_kind == 'unit':
            unit = tx.organization.units.get(body.target_id)
            if unit is None or unit.status != 'active':
                raise InvalidRequestError('目标部门不可用')
        if body.due_at is not None and body.due_at <= self.now():
            raise InvalidRequestError('交接期限必须晚于当前时间')

    def selected_revisions(self, tx: CoordinationTransaction, who: Principal, shared_id: str,
                           revisions: tuple[str, ...]) -> tuple[str, ...]:
        """原生所选每个版本必须当前ready且属于同一共享事项。"""
        for identifier in revisions:
            revision = tx.shared.revisions.get(identifier)
            if revision is None or revision.matter_id != shared_id:
                raise InvalidRequestError('所选资料版本不可用')
            self.materials.readable(tx, who, shared_id, revision.material_id, revision.revision)
        return tuple(dict.fromkeys(revisions))

    @staticmethod
    def policy_version(tx: CoordinationTransaction, identifier: str | None) -> int | None:
        """只能引用有权管理员预配的当前规则，不接受客户端审批人集合。"""
        if identifier is None:
            return None
        policy = tx.collaboration.policies.get(identifier)
        if policy is None or policy.status != 'active':
            raise InvalidRequestError('审批策略尚未准备')
        return policy.version

    def receivers(self, tx: CoordinationTransaction, handoff: Handoff) -> tuple[str, ...]:
        """当前资格必须属于已锁定员工集合，组织并发变化要求重新核对。"""
        needed = set(participant_ids(tx, handoff, self.now()))
        if not needed <= tx.participant_epochs.keys():
            raise ConflictError('受理资格已变化，请重新核对')
        users=tuple(user for user in sorted(needed) if eligible(tx, handoff, user, self.now()))
        if len(users)>100:
            raise ConflictError('当前部门候选超过试点上限，请明确指定接收员工')
        return users

    @staticmethod
    def digest(handoff: Handoff) -> str:
        """摘要冻结完整输入和目标身份，未决审批不能授权变更后的工作。"""
        values = handoff.model_dump(mode='json', include={'shared_id', 'target_kind', 'target_id', 'target_epoch',
            'title', 'summary', 'content_snapshot', 'material_revision_ids', 'source_access_epoch', 'source_grant_id',
            'source_grant_epoch', 'policy_id', 'policy_version'})
        return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def receipt(handoff: Handoff) -> dict[str, JsonValue]:
        """回执不缓存正文或授予永久可见性。"""
        return {key: value for key, value in handoff.model_dump(mode='json').items()
            if key in ('id', 'version', 'status', 'assignee_id', 'delivery_version', 'blocked_reason')}

    @asynccontextmanager
    async def transaction(self, who: Principal, identifier: str, *, extra: tuple[str, ...] = ()) -> AsyncIterator[tuple[CoordinationTransaction, Handoff]]:
        """先确定候选锁集，再按租户及排序员工锁序重查当前业务。"""
        assert who.user_id is not None
        async with self.store.transaction(who.require_tenant(), participants=(who.user_id,)) as read:
            handoff = read.collaboration.handoffs.get(identifier)
            if handoff is None or not can_view(read, who, handoff, self.now()):
                raise NotFoundError('交接不存在')
            participants = tuple(sorted({who.user_id, *participant_ids(read, handoff, self.now()), *extra}))
        inactive = tuple(p for p in participants if p != who.user_id)
        async with self.store.transaction(who.require_tenant(), participants=participants, allow_inactive=inactive) as tx:
            await self.shared.actor(who)
            handoff = tx.collaboration.handoffs[identifier]
            if not can_view(tx, who, handoff, self.now()):
                raise NotFoundError('交接不存在')
            yield tx, handoff

    async def healthy(self, tx: CoordinationTransaction, handoff: Handoff) -> str | None:
        """缺资格只阻塞，不自动改派；已结束业务保留合法历史状态。"""
        if handoff.blocked_reason is not None:
            return handoff.blocked_reason
        if not await source_valid(tx, handoff, self.materials):
            return 'source_access_revoked'
        if handoff.status not in ('delivered','signed_off','returned','cancelled') and handoff.target_kind == 'user' and handoff.target_epoch != tx.participant_epochs.get(handoff.target_id):
            return 'recipient_unavailable'
        if handoff.status not in ('delivered','signed_off','returned','cancelled') and handoff.target_kind=='unit' and not self.receivers(tx,handoff):
            return 'recipient_unavailable'
        if handoff.status not in ('delivered','signed_off', 'returned', 'cancelled') and handoff.assignee_id is not None and not assignee_current(tx, handoff, handoff.assignee_id, self.now()):
            return 'assignee_authority_revoked'
        return None

    def block(self, tx: CoordinationTransaction, who: Principal, handoff: Handoff, reason: str) -> Handoff:
        """永久阻塞在退出事务前保存，之后恢复职责也不能无声复活原责任。"""
        if handoff.blocked_reason is not None:
            return handoff
        blocked = handoff.model_copy(update={'blocked_reason': reason, 'version': handoff.version + 1, 'updated_at': self.now()})
        tx.collaboration.handoffs[handoff.id] = blocked
        record_event(tx, who, blocked, 'blocked', handoff.status, self.now(), recipients=(handoff.requester_id,))
        return blocked

    async def get(self, who: Principal, identifier: str) -> dict[str, JsonValue]:
        """队列只读摘要，原资料正文仅由当前合格承办人或发起人读取。"""
        who = await self.shared.actor(who)
        async with self.transaction(who, identifier) as (tx, handoff):
            reason = await self.healthy(tx, handoff)
            if reason:
                handoff = self.block(tx, who, handoff, reason)
            return self.view(tx, who, handoff)

    def view(self, tx: CoordinationTransaction, who: Principal, handoff: Handoff) -> dict[str, JsonValue]:
        """服务端给出当前动作和已过滤历史，不以隐藏按钮替代权限。"""
        from xuanpu_server.handoffs.views import view
        return view(self, tx, who, handoff)

    async def listing(self, who: Principal, *, view: str = 'received', limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """当前员工队列在分页前过滤，不能另传员工覆盖身份。"""
        who = await self.shared.actor(who)
        assert who.user_id is not None
        if view not in ('sent', 'received', 'assigned', 'department_queue'):
            raise InvalidRequestError('交接视图无效')
        async with self.store.transaction(who.require_tenant(), participants=(who.user_id,)) as read:
            identifiers = [h.id for h in read.collaboration.handoffs.values()
                if self.matches(read, who, h, view) and can_view(read, who, h, self.now())]
            participants = {who.user_id}
            for identifier in identifiers:
                participants.update(participant_ids(read, read.collaboration.handoffs[identifier], self.now()))
        async with self.store.transaction(who.require_tenant(), participants=tuple(sorted(participants)),
                allow_inactive=tuple(sorted(participants - {who.user_id}))) as tx:
            await self.shared.actor(who)
            records: list[dict[str, JsonValue]] = []
            for identifier in identifiers:
                handoff = tx.collaboration.handoffs[identifier]
                if not self.matches(tx, who, handoff, view) or not can_view(tx, who, handoff, self.now()):
                    continue
                reason = await self.healthy(tx, handoff)
                if reason:
                    handoff = self.block(tx, who, handoff, reason)
                records.append({'id': handoff.id, 'version': handoff.version, 'status': handoff.status,
                    'title': handoff.title if handoff.blocked_reason is None else ('已结束工作的历史记录' if handoff.status in ('signed_off','returned','cancelled') else '交接待重新安排'), 'blocked_reason': handoff.blocked_reason})
            return {**page(records, limit=limit, cursor=cursor), 'enabled': self.config.allows(tx.tenant_id)}

    def matches(self, tx: CoordinationTransaction, who: Principal, handoff: Handoff, view: str) -> bool:
        """列表角色使用当前员工，原承办保留解困元数据，不能补读失效正文。"""
        assert who.user_id is not None
        if view == 'sent':
            return handoff.requester_id == who.user_id
        if view == 'assigned':
            return handoff.assignee_id == who.user_id and handoff.assignee_epoch == tx.participant_epochs.get(who.user_id)
        target = eligible(tx, handoff, who.user_id, self.now())
        return target and ((view == 'received' and handoff.target_kind == 'user') or
            (view == 'department_queue' and handoff.target_kind == 'unit' and handoff.status == 'submitted'))
