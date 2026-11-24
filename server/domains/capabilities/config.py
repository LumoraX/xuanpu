"""能力交付只接受明确开启的租户，空名单始终关闭。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class CapabilityDeliveryConfig(BaseModel):
    """有界目录与15分钟预检；配置只能从平台装配注入。"""
    model_config = ConfigDict(frozen=True, extra='forbid')
    enabled: StrictBool = False
    tenants: tuple[str, ...] = Field(default=(), max_length=1000)
    max_revisions: int = Field(default=1000, ge=1, le=10000)
    max_previews: int = Field(default=1000, ge=1, le=10000)
    preview_seconds: int = Field(default=900, ge=60, le=900)
    probe_seconds: float = Field(default=30, ge=1, le=60)

    def allows(self, tenant: str) -> bool:
        """不能借空名单推导全企业开启。"""
        return self.enabled and tenant in self.tenants
