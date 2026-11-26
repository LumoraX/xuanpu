"""xuanpu 侧 Responses 原生 web_search 透传测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import taifeng
from taifeng.loop.event import (
    AssistantCitations,
    ExecutionIdentity,
    WebSearchCompleted,
    WebSearchStarted,
)

from server.api.history import history_view
from server.core.controlplane.models import AuditRecord
from server.core.errors import SharedAccessRevokedError
from server.engine.runtime.assembler import _web_search_config
from server.engine.runtime.source_guard import SourceModelClient
from server.engine.tracing.events import drafts
from spec.errors import ManifestError
from spec.loader import parse_manifest
from spec.manifest import WebSearchSpec


def test_manifest_parses_model_web_search_bool_and_object() -> None:
    """xuanpu.yaml 的 spec.model.webSearch 支持布尔与对象形态，并校验域名。"""
    base = """
apiVersion: xuanpu/v1
kind: Package
metadata:
  name: researcher
  version: 1.0.0
spec:
  skills:
    entries: [main]
"""
    default_manifest = parse_manifest(base)
    assert default_manifest.spec.model.web_search is False
    assert _web_search_config(default_manifest.spec.model.web_search) is None

    bool_manifest = parse_manifest(base + "  model:\n    tier: deep\n    webSearch: true\n")
    assert bool_manifest.spec.model.web_search is True
    assert _web_search_config(bool_manifest.spec.model.web_search) == taifeng.WebSearchConfig()

    obj_manifest = parse_manifest(
        base
        + "  model:\n"
        + "    tier: deep\n"
        + "    webSearch:\n"
        + "      searchContextSize: high\n"
        + "      allowedDomains: [docs.python.org, api-docs.deepseek.com]\n"
    )
    assert isinstance(obj_manifest.spec.model.web_search, WebSearchSpec)
    assert _web_search_config(obj_manifest.spec.model.web_search) == taifeng.WebSearchConfig(
        search_context_size="high",
        allowed_domains=("docs.python.org", "api-docs.deepseek.com"),
    )

    with pytest.raises(ManifestError):
        parse_manifest(
            base
            + "  model:\n"
            + "    webSearch:\n"
            + "      allowedDomains: [https://bad.example.com]\n"
        )


async def test_protected_source_model_client_blocks_native_web_search() -> None:
    """共享只读受保护模式下，即使 skill 声明 web_search 也会在模型出口被拦截。"""
    sim = taifeng.SimClient(
        turns=[taifeng.SimTurn(text="ok")],
        capabilities=taifeng.ModelCapabilities(
            input_modalities=frozenset({"text"}),
            provider="sim",
            protocol="responses",
            supports_web_search=True,
        ),
    )

    async def ok_check() -> tuple[str, ...]:
        return ("ctx-1",)

    guarded = SourceModelClient(sim, ok_check, protected=True)
    req = taifeng.ApiRequest(
        model="m",
        messages=[taifeng.ApiMessage(role="user", content="secret")],
        web_search=taifeng.WebSearchConfig(),
    )
    async with guarded.session(cancel=taifeng.CancellationToken()) as sess:
        with pytest.raises(SharedAccessRevokedError, match="原生联网搜索"):
            async for _ in sess.stream(req):
                pass
    assert len(sim.ledger.requests()) == 0


def test_tracing_drafts_map_web_search_events_to_tool_spans() -> None:
    """web_search_started / web_search_completed 映射为 tl.<call_id> 工具 span。"""
    identity = ExecutionIdentity(
        execution_id="exec_1",
        parent_execution_id=None,
        attempt_id="att_1",
        skill_id="research",
        thread_id="thread_1",
    )
    started = taifeng.EventMsg(
        submission_id="sub_1",
        msg=WebSearchStarted(data={"call_id": "ws_42", "status": "searching"}),
        execution=identity,
    )
    d1 = drafts(started, root_kind="artisan", ref="s1")
    assert len(d1) == 1
    assert d1[0].span_id == "tl.ws_42"
    assert d1[0].kind == "tool"
    assert d1[0].name == "web_search"
    assert d1[0].state == "running"

    completed = taifeng.EventMsg(
        submission_id="sub_1",
        msg=WebSearchCompleted(
            data={
                "call_id": "ws_42",
                "status": "completed",
                "query": "DeepSeek",
                "sources": [{"url": "https://api-docs.deepseek.com"}],
            }
        ),
        execution=identity,
    )
    d2 = drafts(completed, root_kind="artisan", ref="s1")
    assert len(d2) == 1
    assert d2[0].span_id == "tl.ws_42"
    assert d2[0].state == "completed"


def test_history_view_projects_web_search_and_citations() -> None:
    """会话历史视图保留 web_search_completed 摘要与 assistant_citations。"""
    now = datetime.now(UTC).timestamp()
    ws_rec = AuditRecord(
        audit_id=1,
        tenant_id="t1",
        source="engine",
        kind="web_search_completed",
        at=now,
        actor="engine",
        session_id="s1",
        data={
            "call_id": "ws_1",
            "status": "completed",
            "query": "DeepSeek",
            "sources": [{"url": "https://api-docs.deepseek.com"}],
            "internal_extra": "hidden",
        },
    )
    ws_view = history_view(ws_rec)
    assert ws_view["kind"] == "web_search_completed"
    assert ws_view["data"] == {
        "call_id": "ws_1",
        "status": "completed",
        "query": "DeepSeek",
        "sources": [{"url": "https://api-docs.deepseek.com"}],
    }

    cite_msg = AssistantCitations(
        data={"citations": [{"url": "https://api-docs.deepseek.com", "title": "Docs"}]}
    )
    cite_rec = AuditRecord(
        audit_id=2,
        tenant_id="t1",
        source="engine",
        kind=cite_msg.kind,
        at=now,
        actor="engine",
        session_id="s1",
        data={**cite_msg.data, "submission_id": "sub_1"},
    )
    cite_view: dict[str, Any] = history_view(cite_rec)
    assert cite_view["kind"] == "assistant_citations"
    assert cite_view["data"] == {
        "citations": [{"url": "https://api-docs.deepseek.com", "title": "Docs"}]
    }
