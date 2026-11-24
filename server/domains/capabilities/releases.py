"""按员工发布、停止、撤销与回退（R4-C1）。

- 预览只读：核对安装与评测证据（同一能力修订、同一安装配置摘要、评测通过且未过期）、
  固定员工名单（1–100 人，含生命周期代次）、与名单重叠的生效发布；给出确认用的摘要；
- 发布须带预览摘要并明确确认：在同一事务里重算一遍，摘要不符 409（期间有人改了状态）；
  名单与现有生效发布重叠时必须在 ``supersedes`` 里列出这些发布，它们随之停止（retired）；
  首次发布把安装置为 ready——ready 只能经发布确立，安装状态接口仍拒绝直接置 ready；
- 停止（retire）只阻止新受理，已绑定的工作按原授权收尾；撤销（revoke）代次加一，已绑定的
  工作也不能继续；两者都不改旧任务的包、配置或输入；
- 回退就是用旧的、仍有效的安装配置与评测证据再发布一次，同样走预览与确认，不改写旧任务。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from server.core.errors import ConflictError, InvalidRequestError, NotFoundError
from server.core.ids import new_id

from .delivery import fingerprint
from .models import CapabilityRelease, ReleaseTarget

if TYPE_CHECKING:
    from pydantic import JsonValue

    from server.core.coordination.models import CoordinationTransaction
    from server.core.tenancy import Principal

    from .models import CapabilityState
    from .schemas import ReleaseInput, ReleasePreviewInput, ReleaseStateInput
    from .service import CapabilityService

EVIDENCE_SECONDS = 7 * 86400  # 评测报告有效期：过期后不能用于新发布


def release_view(record: CapabilityRelease) -> dict[str, JsonValue]:
    """发布视图：精确修订、安装配置、证据、名单与状态。"""
    return {'id': record.id, 'capability_id': record.capability_id,
            'capability_revision': record.capability_revision, 'installation_id': record.installation_id,
            'config_digest': record.config_digest, 'evaluation_id': record.evaluation_id,
            'targets': [t.model_dump(mode='json') for t in record.targets], 'status': record.status,
            'epoch': record.epoch, 'version': record.version, 'supersedes': record.supersedes,
            'created_at': record.created_at}


class CapabilityReleases:
    """发布的预览、确认、停止、撤销与查询。"""

    def __init__(self, service: CapabilityService) -> None:
        """复用能力服务的管理员复核、仓储与时钟。"""
        self.service = service

    async def _targets(self, tenant: str, targets: list[ReleaseTarget]) -> tuple[ReleaseTarget, ...]:
        """名单去重、1–100 人；每人须在职且代次与确认时一致。"""
        if not 1 <= len(targets) <= 100 or len({t.user_id for t in targets}) != len(targets):
            raise InvalidRequestError('发布名单须为 1–100 名不重复的员工')
        for target in targets:
            user = await self.service.organization.identity.get_user(tenant, target.user_id)
            if user is None or user.status != 'active' or user.lifecycle_epoch != target.user_epoch:
                raise ConflictError('名单里有员工不存在、已停用或代次已变化，请刷新后重新确认')
        return tuple(sorted(targets, key=lambda t: t.user_id))

    def _evidence(self, state: CapabilityState, capability_id: str, installation_id: str,
                  evaluation_id: str) -> tuple[str, int, str]:
        """安装与评测须对得上：评测针对的就是这个安装的某个配置快照（摘要与修订一致）、评测通过且未过期。

        发布固定评测时的配置快照，不要求是安装当前的配置：升级后仍可用旧修订的配置与证据回退。
        """
        installation = state.installations.get(installation_id)
        if installation is None or installation.capability_id != capability_id:
            raise NotFoundError('安装不存在')
        if installation.status not in ('staged', 'ready'):
            raise ConflictError('安装已停止，不能发布')
        evaluation = state.evaluations.get(evaluation_id)
        if evaluation is None or evaluation.capability_id != capability_id:
            raise NotFoundError('评测不存在')
        config = state.configurations.get(evaluation.config_id)
        if (config is None or config.capability_id != capability_id or evaluation.config_digest != config.digest
                or evaluation.capability_revision != config.revision or evaluation.installation_id != installation.id):
            raise ConflictError('评测证据与这个安装的配置不一致，请重新评测')
        if evaluation.status != 'passed':
            raise ConflictError('评测未通过，不能发布')
        if evaluation.finished_at is None or self.service.now() - evaluation.finished_at > EVIDENCE_SECONDS:
            raise ConflictError('评测报告已过期，请重新评测')
        return config.id, config.revision, config.digest

    @staticmethod
    def _overlapping(state: CapabilityState, capability_id: str,
                     targets: tuple[ReleaseTarget, ...]) -> list[CapabilityRelease]:
        """与名单重叠的生效发布（同一员工只能命中一个生效发布）。"""
        users = {t.user_id for t in targets}
        return sorted((r for r in state.releases.values()
                       if r.capability_id == capability_id and r.status == 'active'
                       and users & {t.user_id for t in r.targets}), key=lambda r: r.id)

    def _preview(self, state: CapabilityState, capability_id: str, installation_id: str, evaluation_id: str,
                 targets: tuple[ReleaseTarget, ...]) -> dict[str, JsonValue]:
        """预览内容与确认摘要（摘要覆盖安装版本、证据、名单与需停止的发布及其版本）。"""
        config_id, revision, config_digest = self._evidence(state, capability_id, installation_id, evaluation_id)
        overlapping = self._overlapping(state, capability_id, targets)
        installation = state.installations[installation_id]
        current = {t.user_id: r for r in overlapping for t in r.targets}
        changes: list[JsonValue] = [{
            'user_id': t.user_id, 'from_revision': current[t.user_id].capability_revision if t.user_id in current else None,
            'to_revision': revision} for t in targets]
        basis = {'installation': [installation.id, installation.version], 'evaluation': evaluation_id,
                 'config_digest': config_digest, 'targets': [t.model_dump(mode='json') for t in targets],
                 'supersedes': [[r.id, r.version] for r in overlapping]}
        return {'digest': fingerprint(basis), 'capability_revision': revision, 'config_id': config_id,
                'installation_status': installation.status, 'changes': changes,
                'supersedes': [r.id for r in overlapping]}

    async def preview(self, who: Principal, capability_id: str, body: ReleasePreviewInput) -> dict[str, JsonValue]:
        """只读预览：不写任何记录。"""
        s = self.service
        who = await s.admin(who, mutation=True)
        tenant = who.require_tenant()
        targets = await self._targets(tenant, body.targets)
        state, _ = await s.store.capability_snapshot(tenant)
        return self._preview(state, capability_id, body.installation_id, body.evaluation_id, targets)

    async def release(self, who: Principal, capability_id: str, body: ReleaseInput) -> dict[str, JsonValue]:
        """确认发布：事务内重算预览，摘要与需停止的发布都须与确认时一致。"""
        s = self.service
        who = await s.admin(who, mutation=True)
        tenant = who.require_tenant()
        targets = await self._targets(tenant, body.targets)
        payload = {'kind': 'capability.release', 'capability_id': capability_id, **body.model_dump(mode='json')}
        async with s.store.transaction(tenant, participants=s.participants(who)) as tx:
            await s.admin(who, mutation=True)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            state = tx.state.capabilities
            preview = self._preview(state, capability_id, body.installation_id, body.evaluation_id, targets)
            if preview['digest'] != body.preview_digest:
                raise ConflictError('发布依据已变化，请重新预览并确认')
            if sorted(body.supersedes) != preview['supersedes']:
                raise ConflictError('名单与现有生效发布重叠，须在 supersedes 里明确列出要停止的发布')
            record = self._commit(tx, who, capability_id, body, targets, preview)
            result = release_view(record)
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    def _commit(self, tx: CoordinationTransaction, who: Principal, capability_id: str, body: ReleaseInput,
                targets: tuple[ReleaseTarget, ...], preview: dict[str, JsonValue]) -> CapabilityRelease:
        """停止被取代的发布、把安装置为 ready、写新发布与事件（同一提交）。"""
        s = self.service
        state = tx.state.capabilities
        for identifier in body.supersedes:
            old = state.releases[identifier]
            state.releases[identifier] = old.model_copy(update={'status': 'retired', 'version': old.version + 1})
            tx.emit('capability.release_retired', identifier, old.version + 1, actor=who.actor, at=s.now())
        installation = state.installations[body.installation_id]
        if installation.status != 'ready':
            installation = installation.model_copy(update={'status': 'ready', 'version': installation.version + 1})
            state.installations[installation.id] = installation
            tx.emit('capability.state_changed', installation.id, installation.version, actor=who.actor, at=s.now())
        # 固定评测时的配置快照（预览摘要已覆盖它），不取安装此刻的配置
        config = state.configurations[str(preview['config_id'])]
        record = CapabilityRelease(
            tenant_id=who.require_tenant(), id=new_id('crl'), created_at=s.now(), capability_id=capability_id,
            capability_revision=config.revision, installation_id=installation.id, config_id=config.id,
            config_digest=config.digest, evaluation_id=body.evaluation_id, targets=targets,
            supersedes=body.supersedes[0] if len(body.supersedes) == 1 else None)
        state.releases[record.id] = record
        tx.emit('capability.released', record.id, record.version, actor=who.actor, at=s.now())
        return record

    async def change(self, who: Principal, release_id: str, body: ReleaseStateInput,
                     status: str) -> dict[str, JsonValue]:
        """停止（retired）或撤销（revoked，代次加一）；只能从生效改，带版本 CAS。"""
        s = self.service
        who = await s.admin(who)
        payload = {'kind': 'capability.release_' + status, 'release_id': release_id, **body.model_dump(mode='json')}
        async with s.store.transaction(who.require_tenant(), participants=s.participants(who)) as tx:
            await s.admin(who)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            item = tx.state.capabilities.releases.get(release_id)
            if item is None:
                raise NotFoundError('发布不存在')
            if item.version != body.expected_version or item.status != 'active':
                raise ConflictError('发布已变化或已停止，请刷新后重试')
            changed = item.model_copy(update={
                'status': status, 'version': item.version + 1,
                'epoch': item.epoch + 1 if status == 'revoked' else item.epoch})
            tx.state.capabilities.releases[release_id] = changed
            tx.emit('capability.release_' + status, release_id, changed.version, actor=who.actor, at=s.now())
            result = release_view(changed)
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    async def listing(self, who: Principal, capability_id: str) -> dict[str, JsonValue]:
        """某能力的全部发布（仅管理员）。"""
        s = self.service
        who = await s.admin(who)
        state, _ = await s.store.capability_snapshot(who.require_tenant())
        if not any(r.capability_id == capability_id for r in state.revisions.values()):
            raise NotFoundError('能力不存在')
        items = sorted((r for r in state.releases.values() if r.capability_id == capability_id),
                       key=lambda r: (r.created_at, r.id))
        return {'releases': [release_view(r) for r in items], 'total': len(items)}
