"""安装受理前重新预检；配置、当前状态和审计一次提交。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, NotFoundError
from xuanpu_server.ids import new_id

from .delivery import fingerprint
from .models import Installation, InstallationConfig, InstallPreview
from .schemas import PreviewInput
from .store import current_installation, revision
from .views import installation_view, preview_view

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.tenancy import Principal

    from .preflight import Preflight
    from .schemas import InstallInput, StateInput
    from .service import CapabilityService


class CapabilityInstallations:
    """预检I/O在锁外，提交前身份、密文和连接版本在锁内重查。"""
    def __init__(self, service: CapabilityService, preflight: Preflight) -> None:
        """复用能力领域管理权限与协调仓储。"""
        self.service, self.preflight = service, preflight

    async def preview(self, who: Principal, capability_id: str, body: PreviewInput) -> dict[str, JsonValue]:
        """预检不保存正式安装，不修改包活动指针。"""
        s = self.service
        who = await s.admin(who)
        payload = {'kind': 'capability.preview', 'capability_id': capability_id, **body.model_dump(mode='json')}
        async with s.store.transaction(who.require_tenant(), participants=s.participants(who)) as tx:
            record = revision(tx.state.capabilities, capability_id, body.revision)
            await s.admin(who, mutation=True)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            current = current_installation(tx.state.capabilities, capability_id)
            expected = current.version if current else 0
        evidence, requirements = await self.preflight.evaluate(who.require_tenant(), record.delivery, body,
                                                               timeout=s.config.probe_seconds)
        async with s.store.transaction(who.require_tenant(), participants=s.participants(who)) as tx:
            await s.admin(who, mutation=True)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            self._version(tx, capability_id, expected)
            local, missing = await self.preflight.local(who.require_tenant(), record.delivery, body)
            if any(evidence.get(k) != value for k, value in local.items()):
                raise ConflictError('预检期间依赖版本已变化，请重新核对')
            if len(tx.state.capabilities.previews) >= s.config.max_previews:
                raise ConflictError('安装预检达到上限')
            preview = InstallPreview(tenant_id=who.require_tenant(), id=new_id('ipr'), created_at=s.now(),
                capability_id=capability_id, revision=body.revision, expected_version=expected,
                expires_at=s.now() + s.config.preview_seconds, public_config=body.public_config,
                secret_refs=body.secret_refs, connection_refs=body.connection_refs,
                evidence=evidence, evidence_digest=fingerprint(evidence), requirements=tuple(sorted(set(requirements + missing))),
                diff={'permissions': record.delivery.permissions.model_dump(mode='json'),
                      'public_config_keys': [str(k) for k in sorted(body.public_config)],
                      'secret_slots': [str(k) for k in sorted(body.secret_refs)],
                      'previous_installation_version': expected,
                      'previous_permissions': self._previous_permissions(tx, capability_id)})
            tx.state.capabilities.previews[preview.id] = preview
            result = preview_view(preview)
            tx.emit('capability.preflight', preview.id, 1, actor=who.actor, at=s.now())
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    @staticmethod
    def _previous_permissions(tx: CoordinationTransaction, capability_id: str) -> JsonValue:
        """只比较已安装修订的公开权限，不回显配置或秘密引用。"""
        current = current_installation(tx.state.capabilities, capability_id)
        if current is None:
            return None
        config = tx.state.capabilities.configurations[current.config_id]
        old = revision(tx.state.capabilities, capability_id, config.revision)
        return old.delivery.permissions.model_dump(mode='json')

    async def install(self, who: Principal, capability_id: str, body: InstallInput) -> dict[str, JsonValue]:
        """同操作回原事实，过期、变更或缺条件均不能配置成功。"""
        s = self.service
        who = await s.admin(who)
        payload = {'kind': 'capability.install', 'capability_id': capability_id, **body.model_dump(mode='json')}
        async with s.store.transaction(who.require_tenant(), participants=s.participants(who)) as tx:
            await s.admin(who, mutation=True)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            preview = self._preview(tx, capability_id, body)
            record = revision(tx.state.capabilities, capability_id, preview.revision)
        request = PreviewInput(request_id=body.request_id, revision=preview.revision,
            public_config=preview.public_config, secret_refs=preview.secret_refs, connection_refs=preview.connection_refs)
        evidence, missing = await self.preflight.evaluate(who.require_tenant(), record.delivery, request,
                                                          timeout=s.config.probe_seconds)
        if missing or fingerprint(evidence) != preview.evidence_digest:
            raise ConflictError('依赖、权限或工具已变化，请重新预检')
        async with s.store.transaction(who.require_tenant(), participants=s.participants(who)) as tx:
            await s.admin(who, mutation=True)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            self._preview(tx, capability_id, body)
            local, missing = await self.preflight.local(who.require_tenant(), record.delivery, request)
            if missing or any(preview.evidence.get(k) != value for k, value in local.items()):
                raise ConflictError('安装提交前配置依赖已变化')
            installation = self._stage(tx, capability_id, preview)
            result = installation_view(installation)
            tx.emit('capability.installed', installation.id, installation.version, actor=who.actor, at=s.now())
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    def _stage(self, tx: CoordinationTransaction, capability_id: str, preview: InstallPreview) -> Installation:
        """只追加固定配置并原子替换当前状态，不产生ready或发布。"""
        state = tx.state.capabilities
        old = current_installation(state, capability_id)
        config_revision = old.config_revision + 1 if old else 1
        config = InstallationConfig(tenant_id=tx.tenant_id, id=new_id('icf'), created_at=self.service.now(),
            capability_id=capability_id, revision=preview.revision, config_revision=config_revision,
            public_config=preview.public_config, secret_refs=preview.secret_refs,
            connection_refs=preview.connection_refs, evidence=preview.evidence, digest=preview.evidence_digest)
        state.configurations[config.id] = config
        installation = Installation(tenant_id=tx.tenant_id, id=old.id if old else new_id('ins'),
            version=old.version + 1 if old else 1, created_at=old.created_at if old else self.service.now(),
            capability_id=capability_id, config_id=config.id, config_revision=config_revision)
        state.installations[installation.id] = installation
        return installation

    def _preview(self, tx: CoordinationTransaction, capability_id: str, body: InstallInput) -> InstallPreview:
        """预检、目标能力、时效和CAS必须同时匹配。"""
        preview = tx.state.capabilities.previews.get(body.preview_id)
        if preview is None or preview.capability_id != capability_id:
            raise NotFoundError('安装预检不存在')
        if preview.expires_at <= self.service.now() or preview.requirements:
            raise ConflictError('安装预检已过期或仍有未满足条件')
        if preview.expected_version != body.expected_version:
            raise ConflictError('确认版本与预检不一致')
        self._version(tx, capability_id, body.expected_version)
        return preview

    @staticmethod
    def _version(tx: CoordinationTransaction, capability_id: str, expected: int) -> None:
        """并发安装以当前状态版本选唯一获胜者。"""
        current = current_installation(tx.state.capabilities, capability_id)
        if (current.version if current else 0) != expected:
            raise ConflictError('安装版本已变化，请重新预检')

    async def change(self, who: Principal, identifier: str, body: StateInput) -> dict[str, JsonValue]:
        """关闭新受理后仍可停用，ready必须由后续发布服务确立。"""
        s = self.service
        who = await s.admin(who)
        payload = {'kind': 'capability.state', 'installation_id': identifier, **body.model_dump(mode='json')}
        async with s.store.transaction(who.require_tenant(), participants=s.participants(who)) as tx:
            await s.admin(who)
            item = tx.state.capabilities.installations.get(identifier)
            if item is None:
                raise NotFoundError('安装不存在')
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            if body.status == 'ready':
                raise ConflictError('尚无有效分配和评测发布，请先完成发布检查')
            if item.version != body.expected_version:
                raise ConflictError('安装版本已变化')
            changed = item.model_copy(update={'status': body.status, 'version': item.version + 1})
            tx.state.capabilities.installations[identifier] = changed
            result = installation_view(changed)
            tx.emit('capability.state_changed', identifier, changed.version, actor=who.actor, at=s.now())
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result
