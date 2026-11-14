"""执行追踪配置：只接受明确开启的租户，空名单始终关闭。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, StrictBool

if TYPE_CHECKING:
    from xuanpu_server.tracing.models import SpanKind
    from xuanpu_server.tracing.otel import OtelExport


class ExecutionTraceConfig(BaseModel):
    """追踪开关、单条 trace 的 span 上限与保留天数；只经平台装配注入。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: StrictBool = False
    tenants: tuple[str, ...] = Field(default=(), max_length=1000)
    max_spans_per_trace: int = Field(default=2048, ge=16, le=20000)
    max_attempts_per_span: int = Field(default=64, ge=1, le=1000)
    retention_days: int = Field(default=90, ge=1, le=3650)

    def allows(self, tenant: str) -> bool:
        """不能借空名单推导全企业开启。"""
        return self.enabled and tenant in self.tenants


@dataclass(frozen=True)
class TraceWiring:
    """交给事件枢纽的追踪装配：span 采集配置，加可选的 OTel 导出（平台持有、共享）。

    Attributes:
        root_kind: 根执行 span 的类型；业务会话是 artisan，管家是 butler。
        trace_id: 预先分配的 trace 标识；给了时，该枢纽第一条根 trace 用它（管家据此把
            派工记录挂到同一条 trace 上），之后的根执行照常另开。
    """

    config: ExecutionTraceConfig
    otel: OtelExport | None = None
    root_kind: SpanKind = "artisan"
    trace_id: str | None = None

    def otel_for(self, tenant: str) -> OtelExport | None:
        """只对开启追踪的租户导出；导出与持久 span 用同一份租户名单。"""
        return self.otel if self.config.allows(tenant) else None
