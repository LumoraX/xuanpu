"""提醒建议确认与工作台取消共用的决定键及严格指纹。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import InvalidRequestError
from xuanpu_server.workspace.rules import request_key

if TYPE_CHECKING:
    from xuanpu_server.reminders.models import ReminderRecord


def proposal_decision(record: ReminderRecord, proposal_id: str) -> tuple[str, dict[str, Any]]:
    """使用提醒的完整输入固定确认内容，身份不进入用户可控字段。"""
    request_key(proposal_id)
    if record.request_id != proposal_id + ":create":
        raise InvalidRequestError("建议创建请求标识不匹配")
    key = request_key("proposal:" + proposal_id)
    data = {
        "title": record.title,
        "due_at": record.due_at,
        "timezone": record.timezone,
        "repeat": record.repeat,
        "channels": list(record.channels),
    }
    return key, {"action": "confirm", "data": data}
