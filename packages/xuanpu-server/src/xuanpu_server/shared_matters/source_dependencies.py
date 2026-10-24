"""来源证明记录固定授权足迹；每次使用仍核对当前权限。"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from xuanpu_server.errors import NotFoundError
from xuanpu_server.organization.permissions import descendants, membership_active
from xuanpu_server.shared_matters.models import SharedSourceRef
from xuanpu_server.shared_matters.permissions import access

if TYPE_CHECKING:
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.shared_matters.materials import SharedMaterials
    from xuanpu_server.shared_matters.models import SharedAccessGrant, SourceDependency
    from xuanpu_server.tenancy import Principal


def permission_digest(grant: SharedAccessGrant) -> str | None:
    """同一事项的不同packet不能借某个事项ID互相扩大可见内容。"""
    if grant.scope == 'full':
        return None
    data = [grant.packet_title, grant.include_content, grant.content_snapshot, sorted(grant.material_revisions)]
    encoded = json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def authority_digest(tx: CoordinationTransaction, user: str, grant: SharedAccessGrant, now: float) -> str:
    """固定实际任职及职责身份，撤销后新建同名任职也不能复活原阅读。"""
    scope = grant.subject_id if grant.subject_kind == 'unit' else grant.role_scope_id
    records: list[tuple[str, int, int]] = []
    if scope is not None and (grant.subject_kind == 'unit' or grant.required_org_roles):
        state, epoch = tx.organization, tx.participant_epochs[user]
        records.extend((m.id, m.version, m.epoch) for m in state.memberships.values()
            if m.user_id == user and m.unit_id == scope and membership_active(state, m, now, epoch))
        records.extend((a.id, a.version, a.epoch) for a in state.roles.values()
            if a.user_id == user and a.status == 'active' and a.user_epoch == epoch
            and a.role in grant.required_org_roles and a.scope_id is not None
            and a.scope_id in state.units and state.units[a.scope_id].status == 'active'
            and scope in descendants(state, a.scope_id))
        # 部门和职责覆盖路径恢复时版本已经变化，不能靠轮询撞见撤权窗口。
        current: str | None = scope
        for _ in range(16):
            unit = state.units.get(current or '')
            if unit is None:
                break
            records.append((unit.id, unit.version, 0))
            current = unit.parent_id
    return hashlib.sha256(json.dumps(sorted(records), separators=(',', ':')).encode()).hexdigest()


def source_ref(dependency: SourceDependency) -> SharedSourceRef:
    """只有已持久保存的依赖才导出给消息或受控派生物。"""
    return SharedSourceRef(shared_id=dependency.matter_id, material_revision_id=dependency.material_revision_id,
        access_epoch=dependency.access_epoch, dependency_id=dependency.id, matter_version=dependency.matter_version,
        source_scope=dependency.source_scope, permission_digest=dependency.permission_digest)


def check_dependency(tx: CoordinationTransaction, who: Principal, dependency: SourceDependency, now: float) -> None:
    """身份代次之外还锁定原授权及权限代次，不因再次授权恢复旧上下文。"""
    matter, grants = access(tx, who, dependency.matter_id, now)
    grant = next((g for g in grants if g.id == dependency.grant_id), None)
    if grant is None or grant.epoch != dependency.grant_epoch or matter.access_epoch != dependency.access_epoch:
        raise NotFoundError('共享来源已失效')
    if (grant.scope != dependency.source_scope or permission_digest(grant) != dependency.permission_digest
            or authority_digest(tx, dependency.user_id, grant, now) != dependency.authority_digest):
        raise NotFoundError('共享授权范围已改变')
    if dependency.material_revision_id is not None:
        revision = tx.shared.revisions.get(dependency.material_revision_id)
        if revision is None or revision.matter_id != dependency.matter_id or revision.status != 'ready':
            raise NotFoundError('共享来源材料已撤回')
        material = tx.shared.materials.get(revision.material_id)
        if material is None or material.status == 'withdrawn' or (grant.scope == 'packet' and revision.id not in grant.material_revisions):
            raise NotFoundError('共享来源材料已失效')


def check_reference_scope(tx: CoordinationTransaction, who: Principal, ref: SharedSourceRef, now: float) -> None:
    """派生材料读者必须仍能读取原足迹，不能用不相关packet替代。"""
    matter, grants = access(tx, who, ref.shared_id, now)
    if matter.access_epoch != ref.access_epoch:
        raise NotFoundError('共享来源权限已改变')
    if any(g.scope == 'full' for g in grants):
        return
    if ref.source_scope != 'packet' or ref.permission_digest is None or not any(
            permission_digest(g) == ref.permission_digest for g in grants):
        raise NotFoundError('当前授权不足以读取原来源范围')


def check_publisher_sources(tx: CoordinationTransaction, who: Principal, refs: tuple[SharedSourceRef, ...],
                            now: float, materials: SharedMaterials) -> None:
    """派生发布由服务端消息选择足迹，不能使用失效上下文或他人的依赖记录。"""
    for ref in refs:
        dependency = tx.shared.dependencies.get(ref.dependency_id or '')
        context = tx.shared.contexts.get(dependency.context_id) if dependency is not None else None
        if dependency is None or context is None or context.user_id != who.user_id or context.status != 'active':
            raise NotFoundError('发布来源上下文不可用')
        if context.user_epoch != tx.participant_epochs.get(context.user_id) or source_ref(dependency) != ref:
            raise NotFoundError('发布来源证明已失效')
        check_dependency(tx, who, dependency, now)
        if ref.material_revision_id is not None:
            original = tx.shared.revisions[ref.material_revision_id]
            materials.readable(tx, who, ref.shared_id, original.material_id, original.revision)
