"""共享仓储只修改协调工作单元，提交统一校验复合外键和权属。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError

if TYPE_CHECKING:
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.shared_matters.models import SharedState


def validate_shared(tx: CoordinationTransaction) -> None:
    """内存和数据库共同执行的租户、授权、唯一owner及来源约束。"""
    state = tx.shared
    for field in type(state).model_fields:
        for key, record in getattr(state, field).items():
            if record.tenant_id != tx.tenant_id or record.id != key:
                raise ConflictError('共享记录租户或标识不一致')
    for matter in state.matters.values():
        owners = [m for m in state.members.values() if m.matter_id == matter.id
                  and m.status == 'active' and m.role == 'owner']
        if len(owners) != 1 or owners[0].subject_kind != 'user' or owners[0].subject_id != matter.owner_id:
            raise ConflictError('共享事项必须有且只有一名明确负责人')
    for member in state.members.values():
        grant = state.grants.get(member.grant_id)
        if grant is None or (grant.matter_id, grant.subject_kind, grant.subject_id) != (
                member.matter_id, member.subject_kind, member.subject_id):
            raise ConflictError('共享成员与授权不一致')
        if member.status == 'active' and grant.status != 'active':
            raise ConflictError('有效成员不能引用已撤销授权')
        if grant.scope == 'packet' and member.role != 'viewer':
            raise ConflictError('限定材料授权只能为只读')
    for grant in state.grants.values():
        if grant.matter_id not in state.matters:
            raise ConflictError('共享授权事项不存在')
        if grant.subject_kind == 'unit' and grant.subject_id not in tx.organization.units:
            raise ConflictError('共享授权部门不存在')
        if grant.role_scope_id is not None and grant.role_scope_id not in tx.organization.units:
            raise ConflictError('授权职责部门不存在')
        for revision_id in grant.material_revisions:
            revision = state.revisions.get(revision_id)
            if revision is None or revision.matter_id != grant.matter_id:
                raise ConflictError('限定材料版本不属于当前事项')
    _references(state)


def _references(state: SharedState) -> None:
    """显式校验多级复合关系，避免内存测试掩盖PG外键错误。"""
    for collection in (state.history, state.materials, state.publications, state.dependencies, state.notifications):
        if any(r.matter_id not in state.matters for r in collection.values()):
            raise ConflictError('共享记录引用缺失事项')
    for revision in state.revisions.values():
        material = state.materials.get(revision.material_id)
        if material is None or material.matter_id != revision.matter_id:
            raise ConflictError('材料版本引用缺失材料')
    for publication in state.publications.values():
        material = state.materials.get(publication.material_id)
        if material is None or material.matter_id != publication.matter_id:
            raise ConflictError('发布意图引用缺失材料')
    for dependency in state.dependencies.values():
        context = state.contexts.get(dependency.context_id)
        grant = state.grants.get(dependency.grant_id)
        dependency_revision = state.revisions.get(dependency.material_revision_id or '')
        if context is None or context.user_id != dependency.user_id or grant is None or grant.matter_id != dependency.matter_id:
            raise ConflictError('共享来源依赖不一致')
        if dependency.material_revision_id is not None and (dependency_revision is None or dependency_revision.matter_id != dependency.matter_id):
            raise ConflictError('共享来源材料不一致')
    for notification in state.notifications.values():
        event = state.history.get(notification.event_id)
        if event is None or event.matter_id != notification.matter_id:
            raise ConflictError('共享通知来源不一致')


def validate_history(before: SharedState, after: SharedState) -> None:
    """已提交共享记录不删除；历史事件和固定材料内容不能改写。"""
    for field in type(before).model_fields:
        previous, current = getattr(before, field), getattr(after, field)
        if previous.keys() - current.keys():
            raise ConflictError('共享记录只能撤回或撤销，不能物理删除')
        for key, old in previous.items():
            new = current[key]
            if new == old:
                continue
            if field == 'history' or new.created_at != old.created_at or new.version != old.version + 1:
                raise ConflictError('共享历史或记录版本不可改写')
            if field == 'revisions' and old.model_dump(exclude={'status', 'version', 'updated_at'}) != new.model_dump(exclude={'status', 'version', 'updated_at'}):
                raise ConflictError('已发布材料内容不可覆盖')
