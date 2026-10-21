"""持久执行的服务端边界与保守默认，不受业务包提升。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DurableExecutionConfig(BaseModel):
    """启用前须完成模型上界与网关限制验收；恢复HTTP入口单独登记。"""

    model_config = ConfigDict(extra='forbid', frozen=True)
    enabled: bool = False
    verified_text_bounds: bool = False
    max_attempts: int = Field(default=3, ge=1, le=3)
    recovery_seconds: float = Field(default=1800., ge=60., le=1800.)
    attempt_seconds: float = Field(default=600., ge=1., le=600.)
    lease_seconds: float = Field(default=60., ge=5., le=60.)
    renew_seconds: float = Field(default=20., ge=1., le=20.)
    stateless_http_tools: tuple[str, ...] = ()
