"""公开视图必须排除内部凭据版本、对象键及连接元数据。"""
from __future__ import annotations

import base64
import binascii
import json
import math
from typing import TYPE_CHECKING, cast

from xuanpu_server.errors import InvalidRequestError

if TYPE_CHECKING:
    from pydantic import JsonValue

    from .models import CapabilityRevision, Installation, InstallPreview


def revision_view(record: CapabilityRevision) -> dict[str, JsonValue]:
    """说明可供管理，不显示为已可运行。"""
    return {'capability_id': record.capability_id, 'revision': record.revision,
        'digest': record.digest, 'delivery': record.delivery.model_dump(mode='json'),
        'created_at': record.created_at, 'status': 'not_assigned', 'can_run': False}


def installation_view(record: Installation) -> dict[str, JsonValue]:
    """当前安装只公开版本和状态，不回显配置或秘密引用取值。"""
    return {'id': record.id, 'capability_id': record.capability_id, 'version': record.version,
        'config_revision': record.config_revision, 'status': record.status, 'can_run': False,
        'message': '未通过发布检查，当前不能运行'}


def preview_view(record: InstallPreview) -> dict[str, JsonValue]:
    """完整证据只存在私有数据库；摘要和缺项可明确核对。"""
    credentials = cast('dict[str, JsonValue]', record.evidence.get('credentials', {}))
    return {'id': record.id, 'capability_id': record.capability_id, 'revision': record.revision,
        'expected_version': record.expected_version, 'expires_at': record.expires_at,
        'digest': record.evidence_digest, 'requirements': list(record.requirements),
        'diff': record.diff, 'can_install': not record.requirements, 'can_run': False,
        'credentials': {name: 'configured' if name in credentials else 'missing'
                        for name in record.secret_refs}}


def catalog_page(records: list[CapabilityRevision], *, limit: int, cursor: str | None) -> dict[str, JsonValue]:
    """固定时间与ID游标，数量在分页前计算，非法游标明确拒绝。"""
    if not 1 <= limit <= 100:
        raise InvalidRequestError('每页须为1至100条')
    ordered = sorted(records, key=lambda r: (r.created_at, r.id))
    total = len(ordered)
    if cursor is not None:
        try:
            if len(cursor) > 1024:
                raise ValueError('游标过长')
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if (not isinstance(value, list) or len(value) != 2 or type(value[0]) not in (int, float)
                    or not math.isfinite(value[0]) or not isinstance(value[1], str)):
                raise ValueError('游标类型错误')
            ordered = [r for r in ordered if (r.created_at, r.id) > (value[0], value[1])]
        except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
            raise InvalidRequestError('能力目录游标不合法') from exc
    selected = ordered[:limit]
    after = None
    if len(ordered) > limit:
        last = selected[-1]
        after = base64.urlsafe_b64encode(json.dumps([last.created_at, last.id]).encode()).decode()
    return {'items': [revision_view(r) for r in selected], 'total': total, 'next_cursor': after}
