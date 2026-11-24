"""可选的 OTel 导出（R5-B2）：复用内核 ``OtelTelemetrySink``，按租户各建一个。

- 默认关闭；关闭时不构造任何 sink，也就不建任何连接。
- 端点只由平台管理员配置，不接受租户或业务包自报地址。
- 每个租户一个 sink，资源属性里带 ``xuanpu.tenant``；正文过滤沿用内核（参数、输出、增量、
  完整提示词一律不导出）。
- 内核 sink 不支持按事件附加属性，``xuanpu.trace_id`` 暂时无法写进 OTel span；用
  ``taifeng.execution_id`` 与平台 span（``ex.<execution_id>``）关联，见缺口清单第 32 项。
- 导出失败由内核 sink 自己吞下并记告警，不影响执行。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, StrictBool, field_validator

if TYPE_CHECKING:
    from collections.abc import Callable

    import taifeng

logger = logging.getLogger(__name__)

_RESERVED_ATTRS = frozenset({"xuanpu.tenant", "service.name", "service.version"})


class OtelConfig(BaseModel):
    """``telemetry.otel`` 配置段；只经平台装配注入，不读环境变量。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: StrictBool = False
    endpoint: AnyHttpUrl | None = None
    protocol: Literal["grpc", "http/protobuf"] = "grpc"
    service_name: str = Field(default="xuanpu", min_length=1, max_length=120)
    resource_attributes: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("resource_attributes")
    @classmethod
    def _no_reserved(cls, value: dict[str, str]) -> dict[str, str]:
        """平台自己写的属性不允许被配置覆盖。"""
        clash = _RESERVED_ATTRS & set(value)
        if clash:
            raise ValueError(f"资源属性不能覆盖平台保留键：{sorted(clash)}")
        return value

    def model_post_init(self, _context: Any) -> None:
        """开启时必须给端点：不让 SDK 自己去读 ``OTEL_*`` 环境变量。"""
        if self.enabled and self.endpoint is None:
            raise ValueError("telemetry.otel 开启时必须配置 endpoint")


class OtelExport:
    """按租户懒建内核 OTel sink，平台关闭时统一 flush 并关闭。"""

    def __init__(
        self, config: OtelConfig, *,
        providers: Callable[[dict[str, str]], tuple[Any, Any]] | None = None,
    ) -> None:
        """
        Args:
            config: 已开启的 OTel 配置。
            providers: 测试注入：按资源属性返回 ``(tracer_provider, meter_provider)``；
                不给时由内核按配置建 OTLP 导出。

        Raises:
            ValueError: 配置未开启（关闭时调用方不应构造本类）。
        """
        if not config.enabled:
            raise ValueError("telemetry.otel 未开启时不构造导出器")
        self._config = config
        self._providers = providers
        self._sinks: dict[str, taifeng.OtelTelemetrySink] = {}

    def sink_for(self, tenant_id: str) -> taifeng.OtelTelemetrySink:
        """取（必要时新建）这个租户的 sink。"""
        sink = self._sinks.get(tenant_id)
        if sink is None:
            sink = self._build(tenant_id)
            self._sinks[tenant_id] = sink
        return sink

    def _build(self, tenant_id: str) -> taifeng.OtelTelemetrySink:
        """以平台配置构造内核 sink；资源属性加上租户。"""
        import taifeng

        attrs = {**self._config.resource_attributes, "xuanpu.tenant": tenant_id}
        sink_config = taifeng.OtelSinkConfig(
            service_name=self._config.service_name,
            otlp_endpoint=None if self._config.endpoint is None else str(self._config.endpoint),
            protocol=self._config.protocol,
            resource_attributes=attrs,
        )
        if self._providers is None:
            return taifeng.OtelTelemetrySink(sink_config)
        tracer_provider, meter_provider = self._providers(
            {"service.name": self._config.service_name, **attrs})
        return taifeng.OtelTelemetrySink(
            sink_config, tracer_provider=tracer_provider, meter_provider=meter_provider)

    async def close(self) -> None:
        """flush 并关闭全部 sink；单个失败只记日志，继续关其余的。"""
        sinks, self._sinks = list(self._sinks.values()), {}
        for sink in sinks:
            try:
                await sink.close()
            except Exception:
                logger.exception("关闭 OTel 导出失败")
