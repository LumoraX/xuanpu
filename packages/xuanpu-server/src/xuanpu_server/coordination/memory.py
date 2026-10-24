"""内存协调事务与PG保持取消、回滚和当前身份检查契约。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
from typing import TYPE_CHECKING

from xuanpu_server.collaboration.locks import purpose_participants
from xuanpu_server.collaboration.store import validate_collaboration
from xuanpu_server.controlplane.models import AuditRecord
from xuanpu_server.coordination.models import CoordinationState, CoordinationTransaction
from xuanpu_server.coordination.validation import validate_commit
from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
)
from xuanpu_server.organization.user_profiles import validate_profiles
from xuanpu_server.shared_matters.store import validate_shared

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractContextManager

    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.coordination.outbox import CoordinationEvent
    from xuanpu_server.identity.store import IdentityStore


class InMemoryCoordinationStore:
    """每租户快照串行；生产正确性由PG事务承担。"""
    async def runtime_contexts(self, tenant: str, user: str, session_id: str) -> tuple[str, ...]:
        """只读已提交来源索引；随后仍需协调事务重新检查权限。"""
        state = self._states.get(tenant)
        if state is None:
            return ()
        return tuple(sorted(c.id for c in state.shared.contexts.values()
            if c.user_id == user and c.runtime_session_id == session_id))
    def __init__(self, control: ControlPlaneStore, *, audit: Callable[[list[AuditRecord]], None]) -> None:
        """复用控制面的租户，身份由平台显式绑定。"""
        self._control = control
        self._audit = audit
        self._identity: IdentityStore | None = None
        self._states: dict[str, CoordinationState] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def bind_identity(self, identity: IdentityStore) -> None:
        """在平台装配时注入，不能在请求中替换身份来源。"""
        if self._identity is not None and self._identity is not identity:
            raise RuntimeError('协调事务已绑定其他身份存储')
        self._identity = identity

    def profile_source(self, tenant: str) -> str | None:
        """内存身份更新同步读取已提交来源，不能读取待提交快照。"""
        state = self._states.get(tenant)
        if state is None:
            return None
        policy = next((a for a in state.organization.authority.values()
                       if a.object_type == 'user' and a.field == 'display_name'), None)
        return policy.source if policy is not None else None

    def append_identity_event(self, event: CoordinationEvent) -> None:
        """身份内存写入无await切点，事件与停用同时可见；不启动协调收尾事务。"""
        state = self._states.setdefault(event.tenant_id, CoordinationState())
        state.events.setdefault(event.id, event)

    async def _check(self, tenant: str, participants: tuple[str, ...], allow_inactive: tuple[str, ...]) -> tuple[dict[str, int], dict[str, str]]:
        """当前状态在进入和提交前检查；停用立即生效。"""
        current = await self._control.get_tenant(tenant)
        if current is None:
            raise NotFoundError('租户不存在')
        if current.status != 'active':
            raise PermissionDeniedError('租户已停用')
        if self._identity is None:
            raise RuntimeError('协调事务尚未绑定身份存储')
        epochs: dict[str, int] = {}
        statuses: dict[str, str] = {}
        for user_id in participants:
            user = await self._identity.get_user(tenant, user_id)
            if user is None or (user.status != 'active' and user_id not in allow_inactive):
                raise PermissionDeniedError('参与员工不存在或已停用')
            epochs[user_id], statuses[user_id] = user.lifecycle_epoch, user.status
        return epochs, statuses

    @asynccontextmanager
    async def transaction(self, tenant: str, *, participants: tuple[str, ...] = (),
                          allow_inactive: tuple[str, ...] = (), timeout: float = 5) -> AsyncIterator[CoordinationTransaction]:
        """超时包含等锁时间，异常与取消不替换已提交快照。"""
        ordered = tuple(sorted(set(participants)))
        if not set(allow_inactive) <= set(ordered):
            raise InvalidRequestError('允许停用的员工必须已加入事务')
        async with asyncio.timeout(timeout), self._locks.setdefault(tenant, asyncio.Lock()):
            before = self._states.get(tenant, CoordinationState())
            expanded = purpose_participants(before, ordered)
            permitted = tuple(sorted(set(allow_inactive) | (set(expanded) - set(ordered))))
            epochs, statuses = await self._check(tenant, expanded, permitted)
            state = before.model_copy(deep=True)
            tx = CoordinationTransaction(tenant, expanded, state, epochs, statuses)
            yield tx
            validate_commit(tenant, before, state)
            validate_shared(tx)
            validate_collaboration(tx)
            validate_profiles(tx)
            if await self._check(tenant, expanded, permitted) != (epochs, statuses):
                raise ConflictError('参与员工的身份权限版本已改变')
            await self._foreign_users(tenant, state)
            commit: AbstractContextManager[None] = nullcontext()
            if tx.profile_updates:
                from xuanpu_server.identity.memory import InMemoryIdentityStore
                if not isinstance(self._identity, InMemoryIdentityStore):
                    raise TypeError('内存协调姓名写集要求内存身份存储')
                commit = self._identity.organization_profiles(tx.profile_updates.values())
            with commit:
                self._audit([AuditRecord(tenant_id=tenant, source='control',
                    kind=(state.events[key].kind if '.' in state.events[key].kind else 'organization.' + state.events[key].kind), at=state.events[key].at,
                    actor=state.events[key].actor, data={'event_id': key,
                        'entity_id': state.events[key].entity_id, 'entity_version': state.events[key].entity_version})
                    for key in state.events.keys() - before.events.keys()])
                self._states[tenant] = state.model_copy(deep=True)

    async def _foreign_users(self, tenant: str, state: CoordinationState) -> None:
        """与PG复合身份外键一致；停用历史仍保留，缺失外键则拒绝。"""
        assert self._identity is not None
        users = {m.user_id for m in state.organization.memberships.values()}
        users |= {r.user_id for r in state.organization.roles.values()}
        users |= {m.owner_id for m in state.shared.matters.values()}
        users |= {g.subject_id for g in state.shared.grants.values() if g.subject_kind == 'user'}
        users |= {r.actor_id for r in state.shared.history.values()}
        users |= {r.publisher_id for r in state.shared.materials.values()}
        users |= {r.publisher_id for r in state.shared.revisions.values()}
        users |= {r.publisher_id for r in state.shared.publications.values()}
        users |= {r.user_id for r in state.shared.contexts.values()}
        users |= {r.user_id for r in state.shared.dependencies.values()}
        users |= {r.user_id for r in state.shared.notifications.values()}
        users |= {h.requester_id for h in state.collaboration.handoffs.values()}
        users |= {h.target_id for h in state.collaboration.handoffs.values() if h.target_kind == 'user'}
        users |= {h.assignee_id for h in state.collaboration.handoffs.values() if h.assignee_id is not None}
        users |= {e.actor_id for e in state.collaboration.events.values() if e.actor_id is not None}
        users |= {d.assignee_id for d in state.collaboration.deliveries.values()}
        users |= {a.applicant_id for a in state.collaboration.approvals.values()}
        users |= {step.user_id for a in state.collaboration.approvals.values() for step in a.steps}
        users |= {d.actor_id for d in state.collaboration.decisions.values()}
        users |= {n.user_id for n in state.collaboration.notifications.values()}
        for user in sorted(users):
            if await self._identity.get_user(tenant, user) is None:
                raise NotFoundError('组织关联员工不存在')
