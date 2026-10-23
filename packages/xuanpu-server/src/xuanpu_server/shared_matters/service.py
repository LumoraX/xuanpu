"""共享事项服务要求原生确认，权限、CAS、历史和回执在同一事务。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
)
from xuanpu_server.ids import new_id
from xuanpu_server.shared_matters.models import (
    SharedAccessGrant,
    SharedEvent,
    SharedMatter,
    SharedMatterConfig,
    SharedMember,
)
from xuanpu_server.shared_matters.permissions import access
from xuanpu_server.shared_matters.views import page, project

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.organization.service import OrganizationService
    from xuanpu_server.shared_matters.schemas import (
        ChangeMatterInput,
        CreateMatterInput,
    )
    from xuanpu_server.tenancy import Principal


class SharedMatterService:
    """共享不改变个人工作区，管理员身份不带来内容权限。"""
    def __init__(self, organization: OrganizationService, *, config: SharedMatterConfig | None = None) -> None:
        """复用当前身份校验和协调存储，领域配置默认关闭。"""
        self.organization = organization
        self.store, self.identity, self.now = organization.store, organization.identity, organization.now
        self.config = config or SharedMatterConfig()
        from xuanpu_server.shared_matters.members import SharedMembers
        self.members = SharedMembers(self)

    async def actor(self, who: Principal) -> Principal:
        """直接调用服务也必须有当前有效员工，不接受机器凭据假冒。"""
        return await self.organization.actor(who, employee=True)

    def enabled(self, tenant: str) -> None:
        """关闭后禁止新业务写入，保留已有内容读取与明确撤回。"""
        if not self.config.allows(tenant):
            raise PermissionDeniedError('共享事项新写入尚未启用')

    async def writable(self, tx: CoordinationTransaction, who: Principal, identifier: str, expected: int,
                       *, owner: bool = False) -> SharedMatter:
        """锁内重新核对写权限、负责人可用性和精确事项版本。"""
        matter, _ = access(tx, who, identifier, self.now(), write=True, owner=owner)
        user = await self.identity.get_user(tx.tenant_id, matter.owner_id)
        if user is None or user.status != 'active':
            raise ConflictError('负责人当前不可用，事项不能继续变更')
        if matter.version != expected:
            raise ConflictError('事项已变化，请刷新后重新确认')
        return matter

    def event(self, tx: CoordinationTransaction, who: Principal, matter: SharedMatter, kind: str,
              data: dict[str, JsonValue] | None = None) -> None:
        """正文历史只存共享域；通用outbox和治理审计只存来源引用。"""
        now = self.now()
        assert who.user_id is not None
        identifier = new_id('she')
        tx.shared.history[identifier] = SharedEvent(tenant_id=tx.tenant_id, id=identifier,
            matter_id=matter.id, actor_id=who.user_id, kind=kind, data=data or {}, created_at=now, updated_at=now)
        tx.emit('shared.' + kind, matter.id, matter.version, actor=who.actor, at=now,
                data={'shared_event_id': identifier})

    @staticmethod
    def bump(tx: CoordinationTransaction, matter: SharedMatter, *, now: float,
             changes: dict[str, JsonValue] | None = None, invalidate: bool = False) -> SharedMatter:
        """授权收紧提升access_epoch，旧上下文不能复用原有来源证明。"""
        updated = matter.model_copy(update={**(changes or {}), 'version': matter.version + 1,
            'updated_at': now, 'access_epoch': matter.access_epoch + int(invalidate)})
        tx.shared.matters[matter.id] = updated
        return updated

    async def create(self, who: Principal, body: CreateMatterInput) -> dict[str, JsonValue]:
        """创建事项与唯一owner授权、历史和回执同时提交。"""
        who = await self.actor(who)
        tenant = who.require_tenant()
        assert who.user_id is not None
        payload: dict[str, JsonValue] = {'action': 'shared.create', 'body': body.model_dump(mode='json')}
        async with self.store.transaction(tenant, participants=(who.user_id,)) as tx:
            await self.actor(who)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            self.enabled(tenant)
            if len(tx.shared.matters) >= self.config.max_matters:
                raise ConflictError('共享事项已达到租户上限')
            if not body.title.strip():
                raise InvalidRequestError('事项名称不能为空')
            now, identifier = self.now(), new_id('shr')
            matter = SharedMatter(tenant_id=tenant, id=identifier, owner_id=who.user_id,
                title=body.title, content=body.content, created_at=now, updated_at=now)
            tx.shared.matters[identifier] = matter
            self.owner(tx, matter, who.user_id)
            self.event(tx, who, matter, 'matter_created', {'after': matter.model_dump(mode='json')})
            result: dict[str, JsonValue] = {'id': identifier, 'version': 1, 'status': 'active'}
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    def owner(self, tx: CoordinationTransaction, matter: SharedMatter, user: str) -> None:
        """唯一owner由明确转交创建，不借租户管理角色隐式接管。"""
        now, grant_id, member_id = self.now(), new_id('sgr'), new_id('smb')
        tx.shared.grants[grant_id] = SharedAccessGrant(tenant_id=tx.tenant_id, id=grant_id,
            matter_id=matter.id, subject_kind='user', subject_id=user, subject_epoch=tx.participant_epochs[user],
            created_at=now, updated_at=now)
        tx.shared.members[member_id] = SharedMember(tenant_id=tx.tenant_id, id=member_id,
            matter_id=matter.id, grant_id=grant_id, subject_kind='user', subject_id=user, role='owner',
            created_at=now, updated_at=now)

    async def get(self, who: Principal, identifier: str) -> dict[str, JsonValue]:
        """每次读取先检查当前授权，packet只能得到固定投影。"""
        who = await self.actor(who)
        assert who.user_id is not None
        async with self.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.actor(who)
            matter, grants = access(tx, who, identifier, self.now())
            return project(matter, grants)

    async def listing(self, who: Principal, *, search: str = '', limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """先按当前权限投影，再搜索、计数和分页。"""
        who = await self.actor(who)
        assert who.user_id is not None
        if len(search) > 500:
            raise InvalidRequestError('搜索条件过长')
        async with self.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.actor(who)
            records = []
            for identifier in tx.shared.matters:
                try:
                    matter, grants = access(tx, who, identifier, self.now())
                except NotFoundError:
                    continue
                visible = project(matter, grants)
                if search.casefold() in (str(visible['title']) + '\n' + str(visible['content'])).casefold():
                    records.append(visible)
            return page(records, limit=limit, cursor=cursor)

    async def change(self, who: Principal, identifier: str, body: ChangeMatterInput) -> dict[str, JsonValue]:
        """编辑正文或关闭事项，不产生交付或签收。"""
        who = await self.actor(who)
        assert who.user_id is not None
        payload: dict[str, JsonValue] = {'action': 'shared.change', 'id': identifier, 'body': body.model_dump(mode='json')}
        changes = body.model_dump(mode='json', exclude={'request_id', 'confirmed', 'expected_version'}, exclude_unset=True)
        if not changes or any(v is None for v in changes.values()) or ('title' in changes and not str(changes['title']).strip()):
            raise InvalidRequestError('须明确有效的事项变更')
        async with self.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.actor(who)
            access(tx, who, identifier, self.now(), write=True)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            if changes != {'status': 'archived'}:
                self.enabled(tx.tenant_id)
            before = await self.writable(tx, who, identifier, body.expected_version)
            matter = self.bump(tx, before, now=self.now(), changes=changes)
            self.event(tx, who, matter, 'matter_changed', {'before': before.model_dump(mode='json'), 'after': matter.model_dump(mode='json')})
            result: dict[str, JsonValue] = {'id': matter.id, 'version': matter.version, 'status': matter.status}
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    async def history(self, who: Principal, identifier: str, *, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """只有当前整事项成员可读正文历史，packet不得枚举其他事件。"""
        who = await self.actor(who)
        assert who.user_id is not None
        async with self.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.actor(who)
            access(tx, who, identifier, self.now(), full=True)
            return page([r.model_dump(mode='json') for r in tx.shared.history.values()
                         if r.matter_id == identifier], limit=limit, cursor=cursor)
