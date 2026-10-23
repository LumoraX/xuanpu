"""组织能力的双重灰度开关，默认不开放新写入。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class OrganizationConfig(BaseModel):
    """关闭新写仍允许读取、撤销和停用。"""
    model_config = ConfigDict(frozen=True, extra='forbid')
    enabled: bool = False
    tenants: tuple[str, ...] = ()

    def allows(self, tenant: str) -> bool:
        """必须全局开关和租户白名单同时命中。"""
        return self.enabled and tenant in self.tenants
