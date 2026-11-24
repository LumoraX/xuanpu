"""事务内幂等回执；同键异参明确冲突。"""
from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, JsonValue


class CoordinationReceipt(BaseModel):
    """回执与领域数据同连接提交，不设自动过期窗口。"""
    model_config = ConfigDict(frozen=True, extra='forbid')
    actor: str
    request_id: str
    fingerprint: str
    result: dict[str, JsonValue]


def fingerprint(payload: dict[str, JsonValue]) -> str:
    """规范化后摘要，不在回执中存放原始请求或凭据。"""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def receipt_key(actor: str, request_id: str) -> str:
    """无分隔符碰撞的内部复合键。"""
    return json.dumps([actor, request_id], ensure_ascii=False)
