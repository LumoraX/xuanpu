"""服务端执行空间：每个员工的每项任务或普通会话都有独立工作区。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionScope:
    """只由已授权持久记录构造，不接收客户端路径或scope。"""

    tenant_id: str
    owner_id: str
    execution_id: str

    def __post_init__(self) -> None:
        """拒绝缺失或无限长度的内部标识，目录始终只使用摘要。"""
        if any(not isinstance(value, str) or not value.strip() or len(value) > 512
               for value in (self.tenant_id, self.owner_id, self.execution_id)):
            raise ValueError("执行空间必须有有界租户、员工和任务标识")

    def storage_key(self) -> str:
        """长度固定、不可作路径解析的稳定摘要。"""
        raw = json.dumps([self.tenant_id, self.owner_id, self.execution_id],
                         ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()
