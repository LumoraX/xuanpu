"""能力受理与续跑复核（R4-B1）：所有入口共用同一能力决策。

- 未纳管的包（没有任何入口登记为能力）照旧走原路径，这里一概不管；
- 纳管包的新受理（显式建会话、对话分流、管家派工、任务）一律经 ``admit``：须员工身份、
  租户开启能力交付、有有效分配与 ready 安装；显式指定的版本须等于决策版本，否则 404；
- 受理后 ``bind`` 写一次调用绑定；续聊、恢复、批准后继续经 ``check`` 对照绑定复核当前资格：
  员工代次、原分配（同一 id 与代次、未撤销未过期）、原安装（ready 或 draining 可收尾，disabled 拒绝）；
- 纳管包的受理对象没有绑定（例如进程在建会话与写绑定之间退出）一律拒绝，不猜依据。

开关关闭不撤掉检查：纳管包不能新建，已绑定的工作按当前资格收尾。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import InvalidRequestError, NotFoundError, PermissionDeniedError

from .bindings import BindingKind, InvocationBinding
from .resolver import capability_for_entry, decide, managed_package, resolvable

if TYPE_CHECKING:
    from xuanpu_server.tenancy import Principal

    from .bindings import BindingStore
    from .delivery import Delivery
    from .resolver import CapabilityDecision
    from .service import CapabilityService

_CONTINUABLE = frozenset({'ready', 'draining'})


class CapabilityAdmission:
    """受理与复核；状态一律从权威记录现读，不用缓存放行。"""

    def __init__(self, service: CapabilityService, bindings: BindingStore) -> None:
        """复用能力服务的仓储、组织与时钟。"""
        self._service = service
        self._bindings = bindings

    async def managed(self, tenant: str, package: str) -> bool:
        """这个包是否已纳管。"""
        state, _ = await self._service.store.capability_snapshot(tenant)
        return managed_package(state, package)

    async def _user_epoch(self, who: Principal) -> int:
        """在职员工的当前生命周期代次；机器身份或停用员工拒绝。"""
        if not who.user_id:
            raise NotFoundError('没有可用的能力')
        await self._service.organization.actor(who, employee=True)
        user = await self._service.organization.identity.get_user(who.require_tenant(), who.user_id)
        if user is None or user.status != 'active':
            raise PermissionDeniedError('员工不可用')
        return int(user.lifecycle_epoch)

    async def admit(self, who: Principal, package: str, entry: str | None,
                    version: str | None) -> CapabilityDecision | None:
        """新受理：未纳管返回 None（走原路径）；纳管则返回决策，否则拒绝。"""
        tenant = who.require_tenant()
        if not await self.managed(tenant, package):
            return None
        if not self._service.config.allows(tenant):
            raise PermissionDeniedError('企业未开启能力交付，已纳管的能力暂不能新建')
        epoch = await self._user_epoch(who)
        assert who.user_id is not None
        # 只读快照：不取锁，可在调用方已持有的事务（如定时任务受理）里安全调用
        state, org = await self._service.store.capability_snapshot(tenant)
        now = self._service.now()
        if entry is None:
            choices = [d for d in resolvable(state, org, user_id=who.user_id, user_epoch=epoch, now=now)
                       if d.package_name == package]
            if len(choices) != 1:
                raise NotFoundError('没有可用的能力') if not choices else InvalidRequestError('请指明入口')
            decision = choices[0]
        else:
            capability_id = capability_for_entry(state, package, entry)
            if capability_id is None:
                raise NotFoundError('没有可用的能力')  # 纳管包里未登记的入口拒绝运行
            decision = decide(state, org, capability_id=capability_id, user_id=who.user_id,
                              user_epoch=epoch, now=now)
        if version is not None and version != decision.package_version:
            raise NotFoundError('没有可用的能力')  # 知道包名和精确版本也不能绕过分配
        return decision

    async def delivery(self, tenant: str, decision: CapabilityDecision) -> Delivery:
        """决策所固定的能力修订的交付描述（输入输出契约来自已登记的修订，不来自客户端）。"""
        state, _ = await self._service.store.capability_snapshot(tenant)
        for revision in state.revisions.values():
            if (revision.capability_id, revision.revision) == (decision.capability_id, decision.capability_revision):
                return revision.delivery
        raise NotFoundError('没有可用的能力')

    async def evaluation_target(self, who: Principal, evaluation_id: str, package: str,
                                entry: str | None) -> tuple[str, str]:
        """评测会话的版本与入口：只给进行中的评测、仅管理员、包与入口须属于被评的能力修订。"""
        await self._service.admin(who)
        state, _ = await self._service.store.capability_snapshot(who.require_tenant())
        evaluation = state.evaluations.get(evaluation_id)
        if evaluation is None or evaluation.status != 'running':
            raise NotFoundError('评测不存在或已结束')
        revision = next(r for r in state.revisions.values()
                        if r.capability_id == evaluation.capability_id and r.revision == evaluation.capability_revision)
        target = revision.delivery.package
        if target.name != package or (entry is not None and entry != target.entry):
            raise NotFoundError('评测不存在或已结束')
        return target.version, target.entry

    async def bind_evaluation(self, who: Principal, session_id: str, evaluation_id: str) -> InvocationBinding:
        """评测会话的绑定：记评测而不记分配与发布；续跑时只认进行中且列名的评测。"""
        state, _ = await self._service.store.capability_snapshot(who.require_tenant())
        evaluation = state.evaluations[evaluation_id]
        config = state.configurations[evaluation.config_id]
        revision = next(r for r in state.revisions.values()
                        if r.capability_id == evaluation.capability_id and r.revision == evaluation.capability_revision)
        package = revision.delivery.package
        assert who.user_id is not None or who.key_id is not None
        binding = InvocationBinding(
            tenant_id=who.require_tenant(), kind='session', ref_id=session_id, user_id=who.actor,
            user_epoch=1, capability_id=evaluation.capability_id, capability_revision=revision.revision,
            package_name=package.name, package_version=package.version, package_digest=package.digest,
            entry=package.entry, installation_id=evaluation.installation_id,
            installation_version=state.installations[evaluation.installation_id].version,
            config_id=config.id, config_revision=config.config_revision, config_digest=config.digest,
            evaluation_id=evaluation_id, created_at=self._service.now())
        return await self._bindings.put(binding)

    async def bind(self, who: Principal, kind: BindingKind, ref_id: str,
                   decision: CapabilityDecision) -> InvocationBinding:
        """写一次调用绑定。"""
        binding = InvocationBinding.model_validate({
            'tenant_id': who.require_tenant(), 'kind': kind, 'ref_id': ref_id,
            'created_at': self._service.now(), **_fields(decision)})
        return await self._bindings.put(binding)

    async def binding(self, tenant: str, kind: BindingKind, ref_id: str) -> InvocationBinding | None:
        """读调用绑定。"""
        return await self._bindings.get(tenant, kind, ref_id)

    async def check(self, tenant: str, package: str, kind: BindingKind, ref_id: str) -> None:
        """续跑复核：纳管包须有绑定，且原依据此刻仍有效；否则拒绝。"""
        if not await self.managed(tenant, package):
            return
        binding = await self._bindings.get(tenant, kind, ref_id)
        if binding is None:
            raise PermissionDeniedError('这项工作没有能力依据，不能继续')
        state, _ = await self._service.store.capability_snapshot(tenant)
        installation = state.installations.get(binding.installation_id)
        if binding.evaluation_id is not None:
            # 隔离评测的会话：只在它所属的评测仍在进行、且评测记录列有这个会话时放行
            evaluation = state.evaluations.get(binding.evaluation_id)
            if evaluation is None or evaluation.status != 'running' or ref_id not in evaluation.sessions:
                raise PermissionDeniedError('评测已结束，这个评测会话不能继续')
            return
        user = await self._service.organization.identity.get_user(tenant, binding.user_id)
        if user is None or user.status != 'active' or user.lifecycle_epoch != binding.user_epoch:
            raise PermissionDeniedError('员工状态已变化，这项工作不能继续')
        now = self._service.now()
        assignment = state.assignments.get(binding.assignment_id or '')
        if (assignment is None or assignment.status != 'active' or assignment.epoch != binding.assignment_epoch
                or (assignment.valid_until is not None and assignment.valid_until <= now)):
            raise PermissionDeniedError('能力分配已撤销或过期，这项工作不能继续')
        if binding.release_id is not None:
            # 正常停止（retired）的发布让原绑定收尾；安全撤销（revoked）后原绑定也不能继续
            release = state.releases.get(binding.release_id)
            if release is None or release.status == 'revoked' or release.epoch != binding.release_epoch:
                raise PermissionDeniedError('能力发布已撤销，这项工作不能继续')
        if installation is None or installation.status not in _CONTINUABLE:
            raise PermissionDeniedError('能力已停用，这项工作不能继续')

    async def filter_catalog(self, who: Principal, catalog: list[dict[str, str]]) -> list[dict[str, str]]:
        """对话目录按员工过滤：纳管包只留本人有决策的入口并换成决策版本；未纳管照旧。"""
        tenant = who.require_tenant()
        state, org = await self._service.store.capability_snapshot(tenant)
        managed = {r.delivery.package.name for r in state.revisions.values()}
        if not managed:
            return catalog
        allowed: dict[tuple[str, str], str] = {}
        if who.user_id and self._service.config.allows(tenant):
            epoch = await self._user_epoch(who)
            for d in resolvable(state, org, user_id=who.user_id, user_epoch=epoch, now=self._service.now()):
                allowed[(d.package_name, d.entry)] = d.package_version
        kept = []
        for item in catalog:
            if item['package'] not in managed:
                kept.append(item)
            elif (item['package'], item['entry']) in allowed:
                kept.append({**item, 'version': allowed[(item['package'], item['entry'])]})
        return kept


def _fields(decision: CapabilityDecision) -> dict[str, object]:
    """决策字段（去掉能力标识键，绑定按能力 id 记）。"""
    from dataclasses import asdict

    values = asdict(decision)
    values.pop('capability_key')
    return values
