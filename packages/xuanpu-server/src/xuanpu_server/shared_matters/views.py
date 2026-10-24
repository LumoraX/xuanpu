"""先按授权投影再搜索统计，限定材料授权不暴露整事项字段。"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from xuanpu_server.errors import InvalidRequestError

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.shared_matters.models import SharedAccessGrant, SharedMatter


def selection_version(matter: SharedMatter, grants: list[SharedAccessGrant]) -> int:
    """限定投影选择号绑定全部授权身份，替换或新增资料包必须重新核对。"""
    if any(g.scope == 'full' for g in grants):
        return matter.version
    encoded = json.dumps(sorted((g.id, g.version, g.epoch) for g in grants), separators=(',', ':'))
    return int.from_bytes(hashlib.sha256(encoded.encode()).digest()[:6], 'big') + 1


def project(matter: SharedMatter, grants: list[SharedAccessGrant]) -> dict[str, JsonValue]:
    """整事项授权返回完整事项；限定授权只返回冻结的可见投影。"""
    if any(g.scope == 'full' for g in grants):
        return matter.model_dump(mode='json')
    ordered = sorted(grants, key=lambda g: (g.created_at, g.id))
    return {'id': matter.id, 'scope': 'packet', 'version': selection_version(matter, grants), 'title': ordered[0].packet_title,
            'content': '\n'.join(dict.fromkeys(g.content_snapshot or '' for g in ordered if g.include_content)),
            'material_revisions': [str(r) for r in sorted({r for g in ordered for r in g.material_revisions})]}


def page(records: list[dict[str, JsonValue]], *, limit: int, cursor: str | None) -> dict[str, JsonValue]:
    """固定ID游标及服务端独立统计，不把限定授权的原事项状态作为旁路。"""
    if not 1 <= limit <= 100 or (cursor is not None and len(cursor) > 160):
        raise InvalidRequestError('分页参数无效')
    ordered = sorted(records, key=lambda r: str(r['id']))
    counts: dict[str, JsonValue] = {status: sum(r.get('status') == status for r in records)
                                    for status in ('active', 'closed', 'archived')}
    remaining = [r for r in ordered if cursor is None or str(r['id']) > cursor]
    selected = remaining[:limit]
    return {'items': [dict(r) for r in selected], 'total': len(records), 'counts': counts,
            'next_cursor': selected[-1]['id'] if len(remaining) > limit else None}
