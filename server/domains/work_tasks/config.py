"""持久执行的服务端边界与保守默认，不受业务包提升。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DurableExecutionConfig(BaseModel):
    """启用前须完成模型上界与网关限制验收；恢复HTTP入口单独登记。"""

    model_config = ConfigDict(extra='forbid', frozen=True)
    enabled: bool = False
    tenant_ids: tuple[str,...] = Field(default=(),max_length=1000)
    verified_text_bounds: bool = False
    max_task_tokens: int = Field(default=150000,ge=1,le=300000)
    max_attempts: int = Field(default=3, ge=1, le=3)
    recovery_seconds: float = Field(default=1800., ge=60., le=1800.)
    attempt_seconds: float = Field(default=600., ge=1., le=600.)
    lease_seconds: float = Field(default=60., ge=5., le=60.)
    renew_seconds: float = Field(default=20., ge=1., le=20.)
    stateless_http_tools: tuple[str, ...] = ()

    @field_validator('tenant_ids', mode='before')
    @classmethod
    def immutable_tenants(cls, value: Any) -> Any:
        """配置名单保持不可变，不从员工请求接收范围。"""
        return tuple(value) if isinstance(value,list) else value

    def allows(self, tenant: str | None) -> bool:
        """只准入显式灰度租户，空名单表示管理员全量开关。"""
        return self.enabled and (not self.tenant_ids or tenant in self.tenant_ids)
