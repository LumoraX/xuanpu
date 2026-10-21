"""个人正文与会话管理元数据分别授权。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xuanpu_server.controlplane.models import SessionRecord
    from xuanpu_server.tenancy import Principal


def can_read_content(principal: Principal, record: SessionRecord) -> bool:
    """只有员工本人或无主系统会话的创建者可以读取和继续其内容。"""
    if principal.tenant_id != record.tenant_id:
        return False
    if record.user_id is not None:
        return principal.user_id == record.user_id
    return principal.user_id is None and principal.actor == record.created_by
