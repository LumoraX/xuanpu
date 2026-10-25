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
