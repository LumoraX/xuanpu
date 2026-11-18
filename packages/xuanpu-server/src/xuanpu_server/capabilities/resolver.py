"""能力决策（R4-B）：只由认证员工与权威记录构造，所有调用路径共用。

一次决策回答「这名员工此刻能否新受理这个能力，按哪个精确修订、安装与分配依据」：

- 员工须在职，直接到人的分配须对得上员工当前生命周期代次（停用重建后旧分配不复活）；
- 岗位或部门分配经员工当前真实任职解析（任职的员工代次、有效期、部门状态都要对），
  且目标岗位或部门的结构版本仍是分配时确认的版本；客户端自报岗位无效；
- 能力须有当前安装且为 ``ready``，安装所用的能力修订须在分配的明确修订范围内；
- 多项有效分配命中同一安装合并为一个依据（取最早的分配，结果稳定）；R4-C 引入发布后，
  命中不同发布须由管理员整理，这里拒绝而不猜。

没有有效依据一律 404（不暴露能力与分配是否存在）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from xuanpu_server.errors import NotFoundError
from xuanpu_server.organization.permissions import membership_active

from .store import current_installation

if TYPE_CHECKING:
    from xuanpu_server.organization.models import OrganizationState

    from .models import CapabilityAssignment, CapabilityRevision, CapabilityState, Installation


@dataclass(frozen=True)
class CapabilityDecision:
    """一次新受理固定下来的依据；保存进调用绑定，之后每次续跑都对照它复核。"""

    capability_id: str
    capability_key: str
    capability_revision: int
    package_name: str
    package_version: str
    package_digest: str
    entry: str
    installation_id: str
    installation_version: int
    config_id: str
    config_revision: int
    config_digest: str
    assignment_id: str
    assignment_epoch: int
    user_id: str
    user_epoch: int
    release_id: str | None = None
    release_epoch: int | None = None


def assignment_matches(assignment: CapabilityAssignment, org: OrganizationState, *, user_id: str,
                       user_epoch: int, now: float) -> bool:
    """这项分配此刻是否覆盖这名员工（只看分配本身与真实任职，不看安装）。"""
    if assignment.status != 'active' or (assignment.valid_until is not None and assignment.valid_until <= now):
        return False
    selector = assignment.selector
    if selector.kind == 'user':
        return selector.id == user_id and selector.user_epoch == user_epoch
    target = (org.positions if selector.kind == 'position' else org.units).get(selector.id)
    if target is None or target.status != 'active' or target.version != selector.target_version:
        return False
    for member in org.memberships.values():
        if member.user_id != user_id or not membership_active(org, member, now, user_epoch):
            continue
        if (member.position_id if selector.kind == 'position' else member.unit_id) == selector.id:
            return True
    return False


def _revision_of(state: CapabilityState, installation: Installation) -> CapabilityRevision:
    """当前安装所用的精确能力修订。"""
    config = state.configurations[installation.config_id]
    return next(r for r in state.revisions.values()
                if r.capability_id == installation.capability_id and r.revision == config.revision)


def decide(state: CapabilityState, org: OrganizationState, *, capability_id: str, user_id: str,
           user_epoch: int, now: float) -> CapabilityDecision:
    """新受理的能力决策；没有有效依据 404。"""
    installation = current_installation(state, capability_id)
    if installation is None or installation.status != 'ready':
        raise NotFoundError('没有可用的能力')
    revision = _revision_of(state, installation)
    candidates = sorted(
        (a for a in state.assignments.values()
         if a.capability_id == capability_id and revision.revision in a.allowed_revisions
         and assignment_matches(a, org, user_id=user_id, user_epoch=user_epoch, now=now)),
        key=lambda a: (a.created_at, a.id))
    if not candidates:
        raise NotFoundError('没有可用的能力')
    chosen = candidates[0]
    config = state.configurations[installation.config_id]
    package = revision.delivery.package
    return CapabilityDecision(
        capability_id=capability_id, capability_key=revision.capability_key,
        capability_revision=revision.revision, package_name=package.name,
        package_version=package.version, package_digest=package.digest, entry=package.entry,
        installation_id=installation.id, installation_version=installation.version,
        config_id=config.id, config_revision=config.config_revision, config_digest=config.digest,
        assignment_id=chosen.id, assignment_epoch=chosen.epoch, user_id=user_id, user_epoch=user_epoch)


def capability_for_entry(state: CapabilityState, package: str, entry: str) -> str | None:
    """包入口归哪个能力（同一租户 package+entry 只归一个能力）；未纳管返回 None。"""
    for revision in state.revisions.values():
        if (revision.delivery.package.name, revision.delivery.package.entry) == (package, entry):
            return revision.capability_id
    return None


def managed_package(state: CapabilityState, package: str) -> bool:
    """包里任一入口已登记为能力即视为纳管（R4-B1：纳管包整包走能力决策）。"""
    return any(r.delivery.package.name == package for r in state.revisions.values())


def resolvable(state: CapabilityState, org: OrganizationState, *, user_id: str, user_epoch: int,
               now: float) -> list[CapabilityDecision]:
    """这名员工此刻能新受理的全部能力（「我的能力」与对话目录的数据来源）。"""
    found = []
    for capability_id in sorted({r.capability_id for r in state.revisions.values()}):
        try:
            found.append(decide(state, org, capability_id=capability_id, user_id=user_id,
                                user_epoch=user_epoch, now=now))
        except NotFoundError:
            continue
    return found
