"""权限过滤后的稳定复合游标与最小目录视图。"""
from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

from xuanpu_server.errors import InvalidRequestError

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.organization.models import OrgRecord

def page(records: list[OrgRecord], *, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
    """总数先于分页计算；游标由创建时间及稳定ID组成。"""
    if not 1 <= limit <= 100:
        raise InvalidRequestError('每页须为1至100条')
    ordered = sorted(records, key=lambda r: (r.created_at, r.id))
    total = len(ordered)
    if cursor is not None:
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], (float, int)) or not isinstance(value[1], str):
                raise ValueError('无效游标')
            after = (float(value[0]), value[1])
        except (ValueError, TypeError, UnicodeError) as exc:
            raise InvalidRequestError('无效的组织目录游标') from exc
        ordered = [r for r in ordered if (r.created_at, r.id) > after]
    selected = ordered[:limit]
    next_cursor = None
    if len(ordered) > limit and selected:
        last = selected[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps([last.created_at, last.id]).encode()).decode()
    return {'items': [r.model_dump(mode='json') for r in selected], 'total': total, 'next_cursor': next_cursor}
