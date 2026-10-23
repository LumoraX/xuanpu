"""稳定来源映射与字段权威检查，禁止姓名推断身份。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError

if TYPE_CHECKING:
    from xuanpu_server.organization.models import OrganizationState, SourceMapping

def mapping(state: OrganizationState, source: str, object_type: str, external_id: str) -> SourceMapping | None:
    """同一安装的外部ID只能指向一个本地稳定实体。"""
    return next((m for m in state.mappings.values() if m.source == source
                 and m.object_type == object_type and m.external_id == external_id), None)

def authority_versions(state: OrganizationState, source: str, object_type: str, fields: tuple[str, ...]) -> dict[str, int]:
    """缺少显式供应商权威时坚持local；不静默覆盖本地字段。"""
    versions = {}
    for field in fields:
        rule = next((a for a in state.authority.values() if a.object_type == object_type and a.field == field), None)
        if rule is None or rule.source != source:
            raise ConflictError('字段由本地或其他来源维护')
        versions[field] = rule.version
    return versions
