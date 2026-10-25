"""能力登记的领域服务；短协调事务与对象校验分开。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, PermissionDeniedError
from xuanpu_server.ids import new_id

from .config import CapabilityDeliveryConfig
from .contracts import validate_package
from .delivery import fingerprint
from .installations import CapabilityInstallations
from .models import CapabilityRevision
from .views import catalog_page, installation_view, revision_view

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pydantic import JsonValue

    from xuanpu_server.organization.service import OrganizationService
    from xuanpu_server.packages import PackageRegistry
    from xuanpu_server.tenancy import Principal

    from .preflight import Preflight
    from .schemas import RegisterInput


class CapabilityService:
    """能力基础不修改PackageRegistry活动版本，也不提供运行授权。"""
    def __init__(self, organization: OrganizationService, packages: PackageRegistry,
                 config: CapabilityDeliveryConfig | None = None, *, preflight: Preflight) -> None:
        """复用当前身份复核、时钟和原子协调仓储。"""
        self.organization, self.packages = organization, packages
        self.store, self.now = organization.store, organization.now
        self.config = config or CapabilityDeliveryConfig()
        self.installations = CapabilityInstallations(self, preflight)

    async def admin(self, who: Principal, *, mutation: bool = False) -> Principal:
        """管理员资格每次复核；关开关后仍可查看历史。"""
        current = await self.organization.actor(who)
        tenant = current.require_tenant_admin()
        if mutation and not self.config.allows(tenant):
            raise PermissionDeniedError('企业尚未开启能力交付配置')
        return current

    async def register(self, who: Principal, body: RegisterInput) -> dict[str, JsonValue]:
        """描述校验在事务外；登记、回执和审计在同一提交。"""
        who = await self.admin(who, mutation=True)
        payload = {'kind': 'capability.register', **body.model_dump(mode='json')}
        async with self.store.transaction(who.require_tenant(), participants=self.participants(who)) as tx:
            await self.admin(who, mutation=True)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
        package = await self.packages.resolve(who.require_tenant(), body.delivery.package.name,
                                              body.delivery.package.version)
        validate_package(body.delivery, package, self.installations.preflight.inventory.defaults)
        async with self.store.transaction(who.require_tenant(), participants=self.participants(who)) as tx:
            await self.admin(who, mutation=True)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            state = tx.state.capabilities
            previous = [r for r in state.revisions.values() if r.capability_key == body.delivery.capability_key]
            digest = fingerprint(body.delivery.model_dump(mode='json'))
            identical = next((r for r in previous if r.digest == digest), None)
            if identical is not None:
                result = revision_view(identical)
            else:
                if len(state.revisions) >= self.config.max_revisions:
                    raise ConflictError('能力修订达到上限')
                self._entry(state.revisions.values(), body)
                record = CapabilityRevision(tenant_id=who.require_tenant(), id=new_id('cvr'),
                    created_at=self.now(), capability_id=previous[0].capability_id if previous else new_id('cap'),
                    capability_key=body.delivery.capability_key, revision=len(previous) + 1,
                    digest=digest, delivery=body.delivery)
                state.revisions[record.id] = record
                tx.emit('capability.registered', record.id, record.revision, actor=who.actor, at=self.now())
                result = revision_view(record)
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    @staticmethod
    def _entry(records: Iterable[CapabilityRevision], body: RegisterInput) -> None:
        """同包入口只归一个稳定能力，能力不能更换身份引用。"""
        target = body.delivery
        for record in records:
            same = (record.delivery.package.name, record.delivery.package.entry) == (target.package.name, target.package.entry)
            if same != (record.capability_key == target.capability_key):
                raise ConflictError('能力标识与包入口已固定映射')

    async def listing(self, who: Principal, *, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """管理列表的总数与当前页无关，不提供员工枚举入口。"""
        who = await self.admin(who)
        async with self.store.transaction(who.require_tenant(), participants=self.participants(who)) as tx:
            await self.admin(who)
            records = list(tx.state.capabilities.revisions.values())
            result = catalog_page(records, limit=limit, cursor=cursor)
            result['installations'] = [installation_view(i) for i in tx.state.capabilities.installations.values()]
            result['configuration_enabled'] = self.config.allows(who.require_tenant())
            return result

    async def mine(self, who: Principal) -> dict[str, JsonValue]:
        """A阶段没有分配或发布事实，员工可运行目录明确为空。"""
        who = await self.organization.actor(who, employee=True)
        async with self.store.transaction(who.require_tenant(), participants=self.participants(who)):
            await self.organization.actor(who, employee=True)
        return {'capabilities': [], 'total': 0, 'status': 'not_assigned',
                'message': '尚未分配可运行能力'}

    @staticmethod
    def participants(who: Principal) -> tuple[str, ...]:
        """机器管理凭据不冒充某位员工。"""
        return (who.user_id,) if who.user_id else ()
