"""可选 OTel 导出：关闭时零连接；开启时按租户导出、父子正确、带租户属性、不含正文。"""

from __future__ import annotations

import contextlib
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
import taifeng
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from xuanpu_server.bootstrap import _otel_export
from xuanpu_server.tracing.config import ExecutionTraceConfig
from xuanpu_server.tracing.otel import OtelConfig, OtelExport

from .test_platform_tracing import _employee

_BODY_KEYS = {"arguments", "output", "delta", "summary", "user_text_preview", "text", "content"}


def _wire(world: Any, export: OtelExport | None, *tenants: str) -> None:
    """测试内替换平台默认参数：追踪名单与 OTel 导出。"""
    runtimes = world.platform.runtimes
    runtimes._defaults = replace(  # noqa: SLF001 —— 测试内装配
        runtimes.defaults, otel_export=export,
        execution_trace=ExecutionTraceConfig(enabled=True, tenants=tenants))


def _collector() -> tuple[OtelExport, InMemorySpanExporter]:
    """本机收集器替身：同步处理器，导出结果留在内存。"""
    exporter = InMemorySpanExporter()

    def providers(attrs: dict[str, str]) -> tuple[TracerProvider, MeterProvider]:
        """每个租户一套 provider，共用一个内存导出器。"""
        resource = Resource.create(attrs)
        tracer = TracerProvider(resource=resource)
        tracer.add_span_processor(SimpleSpanProcessor(exporter))
        return tracer, MeterProvider(resource=resource)

    config = OtelConfig(enabled=True, endpoint="http://collector.invalid:4317")
    return OtelExport(config, providers=providers), exporter


def test_config_rules() -> None:
    """默认关闭；开启必须给端点；保留属性不能被覆盖；关闭时不构造导出器。"""
    assert OtelConfig().enabled is False
    with pytest.raises(ValueError, match="endpoint"):
        OtelConfig(enabled=True)
    with pytest.raises(ValueError, match="保留键"):
        OtelConfig(enabled=True, endpoint="http://c:4317",
                   resource_attributes={"xuanpu.tenant": "x"})
    with pytest.raises(ValueError, match="未开启"):
        OtelExport(OtelConfig())


async def test_disabled_builds_no_sink(world: Any, step: Any, read_turn: Any,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """关闭：装配返回 None，跑一轮对话也不构造任何内核 sink（因而零连接）。"""
    built: list[object] = []
    monkeypatch.setattr(taifeng, "OtelTelemetrySink", lambda *a, **k: built.append(a))
    config = SimpleNamespace(telemetry=SimpleNamespace(otel=OtelConfig()))
    async with contextlib.AsyncExitStack() as stack:
        assert _otel_export(stack, config) is None  # type: ignore[arg-type]
    _wire(world, None, "acme")
    await world.publish()
    alice = await _employee(world, "usr_alice")
    session = await world.platform.sessions.create(alice, package="support-desk")
    world.models.script(step(text="收到"))
    await read_turn(await world.platform.sessions.send_message(alice, session.session_id, "你好"))
    await world.platform.sessions.close(alice, session.session_id)
    assert built == []


async def test_enabled_exports_tree_with_tenant_and_no_body(
    world: Any, step: Any, read_turn: Any,
) -> None:
    """开启：工具 span 挂在 turn 下；资源带租户；属性里没有正文；名单外租户不导出。"""
    export, exporter = _collector()
    _wire(world, export, "acme")
    await world.publish()
    alice = await _employee(world, "usr_alice")
    session = await world.platform.sessions.create(alice, package="support-desk")
    world.models.script(
        step(calls=[("c1", "mcp__crm__search_customer", {"name": "机密客户"})]),
        step(text="查到了"))
    await read_turn(await world.platform.sessions.send_message(alice, session.session_id, "查"))
    await world.platform.sessions.close(alice, session.session_id)
    await export.close()
    spans = exporter.get_finished_spans()
    turns = [s for s in spans if s.name == "taifeng.turn"]
    tools = [s for s in spans if s.name.startswith("taifeng.tool.")]
    assert turns and tools
    assert tools[0].parent is not None
    assert tools[0].parent.span_id in {t.context.span_id for t in turns}
    for span in spans:
        assert span.resource.attributes["xuanpu.tenant"] == "acme"
        assert not _BODY_KEYS & set(span.attributes or {})
        for event in span.events:
            assert not _BODY_KEYS & set(event.attributes or {})
    dumped = json.dumps([dict(s.attributes or {}) for s in spans], ensure_ascii=False)
    assert "机密客户" not in dumped and "查到了" not in dumped


async def test_tenant_outside_trace_list_is_not_exported(
    world: Any, step: Any, read_turn: Any,
) -> None:
    """导出与持久 span 用同一份租户名单。"""
    export, exporter = _collector()
    _wire(world, export, "globex")
    await world.publish()
    alice = await _employee(world, "usr_alice")
    session = await world.platform.sessions.create(alice, package="support-desk")
    world.models.script(step(text="收到"))
    await read_turn(await world.platform.sessions.send_message(alice, session.session_id, "你好"))
    await world.platform.sessions.close(alice, session.session_id)
    await export.close()
    assert exporter.get_finished_spans() == ()
