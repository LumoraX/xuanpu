"""能力写集与历史不变量，在两后端同一协调提交边界执行。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, NotFoundError

if TYPE_CHECKING:
    from .models import CapabilityRevision, CapabilityState, Installation


def revision(state: CapabilityState, capability_id: str, number: int) -> CapabilityRevision:
    """只有本租户已登记的精确修订可被配置。"""
    found = next((r for r in state.revisions.values()
                  if r.capability_id == capability_id and r.revision == number), None)
    if found is None:
        raise NotFoundError('能力修订不存在')
    return found


def current_installation(state: CapabilityState, capability_id: str) -> Installation | None:
    """每个能力只有一个当前安装，旧配置单独不可变保留。"""
    return next((i for i in state.installations.values() if i.capability_id == capability_id), None)


def validate_state(tenant: str, before: CapabilityState, after: CapabilityState) -> None:
    """禁止删历史、覆盖配置、跨租户写集或错配当前配置。"""
    for field in ('revisions', 'previews', 'configurations'):
        old = getattr(before, field)
        new = getattr(after, field)
        if any(key not in new or new[key] != value for key, value in old.items()):
            raise ConflictError('能力历史与预检不可改写')
    for field in type(after).model_fields:
        for key, record in getattr(after, field).items():
            if key != record.id or record.tenant_id != tenant:
                raise ConflictError('能力记录租户或标识不一致')
    _validate_installations(before, after)
    _validate_assignments(before, after)
    _validate_releases(before, after)
    for field in ('batches', 'observations'):
        old_items, new_items = getattr(before, field), getattr(after, field)
        if any(key not in new_items or new_items[key] != value for key, value in old_items.items()):
            raise ConflictError('验收批次与核实观察只能追加，不能改写或删除')


def _validate_installations(before: CapabilityState, after: CapabilityState) -> None:
    """配置外键和当前安装版本在两后端保持同一边界。"""
    if before.installations.keys() - after.installations.keys():
        raise ConflictError('安装记录不能物理删除')
    for identifier, item in after.installations.items():
        config = after.configurations.get(item.config_id)
        if config is None or config.capability_id != item.capability_id or config.config_revision != item.config_revision:
            raise ConflictError('当前安装与配置不一致')
        old = before.installations.get(identifier)
        if old is not None and old != item:
            if (item.version != old.version + 1 or item.capability_id != old.capability_id or
                    item.created_at != old.created_at):
                raise ConflictError('安装更新版本不一致')


def _validate_assignments(before: CapabilityState, after: CapabilityState) -> None:
    """分配不物理删除；目标与修订范围不可改写；撤销不可复活，改动版本逐次加一。"""
    if before.assignments.keys() - after.assignments.keys():
        raise ConflictError('能力分配不能物理删除')
    for identifier, item in after.assignments.items():
        if not any(r.capability_id == item.capability_id for r in after.revisions.values()):
            raise ConflictError('分配引用的能力不存在')
        old = before.assignments.get(identifier)
        if old is None or old == item:
            continue
        if (item.version != old.version + 1 or item.selector != old.selector
                or item.allowed_revisions != old.allowed_revisions or item.capability_id != old.capability_id
                or item.created_at != old.created_at or old.status == 'revoked'):
            raise ConflictError('能力分配只能按版本撤销，不能改写或复活')


def _validate_releases(before: CapabilityState, after: CapabilityState) -> None:
    """评测与发布不物理删除；发布的版本、目标与证据不可改写，只能 active→retired/revoked；
    已结束的评测不再改写。"""
    if before.releases.keys() - after.releases.keys() or before.evaluations.keys() - after.evaluations.keys():
        raise ConflictError('评测与发布记录不能物理删除')
    for identifier, item in after.releases.items():
        old = before.releases.get(identifier)
        if old is None or old == item:
            continue
        frozen = ('capability_id', 'capability_revision', 'installation_id', 'config_id', 'config_digest',
                  'evaluation_id', 'targets', 'supersedes', 'created_at')
        if (item.version != old.version + 1 or any(getattr(item, f) != getattr(old, f) for f in frozen)
                or old.status != 'active' or item.status == 'active'):
            raise ConflictError('发布只能从生效改为停止或撤销，不能改写')
    for identifier, evaluation in after.evaluations.items():
        previous = before.evaluations.get(identifier)
        if previous is not None and previous != evaluation and previous.status != 'running':
            raise ConflictError('已结束的评测不能改写')
