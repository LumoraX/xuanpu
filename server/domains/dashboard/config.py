"""极简入口开关：按租户白名单启用；未启用的租户仍是旧页。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class MinimalEntryConfig(BaseModel):
    """``minimal_entry`` 配置段；只经平台装配注入。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: StrictBool = False
    tenants: tuple[str, ...] = Field(default=(), max_length=1000)

    def allows(self, tenant: str | None) -> bool:
        """不能借空名单推导全企业启用。"""
        return self.enabled and tenant is not None and tenant in self.tenants
