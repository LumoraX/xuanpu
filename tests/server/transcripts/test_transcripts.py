"""双层脱敏本地 Transcript、三态透明跨节点读取与自排查接口测试（ADR 0026）。"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from server.api.app import create_app
from server.api.auth import caller
from server.core.errors import AuthenticationError, NotFoundError
from server.core.objectstore import LocalObjectStore
from server.core.tenancy import Principal
from server.domains.identity.models import UserRecord
from server.engine.butler.config import ButlerConfig
from server.engine.tracing.config import ExecutionTraceConfig
from server.engine.transcripts.models import TranscriptIndexRecord, TranscriptSlice
from server.engine.transcripts.reader import TICKET_HEADER, NodeTicketSigner, TranscriptReader
from server.engine.transcripts.store import MemoryTranscriptIndexStore
from server.engine.transcripts.writer import LocalTranscriptWriter, prune_local_transcripts

if TYPE_CHECKING:
    from pathlib import Path


async def test_local_writer_sanitizes_truncates_and_enforces_permissions(tmp_path: Path) -> None:
    """本地双层落盘严格脱敏、行号对齐、截断大字段并强制 0700/0600 权限。"""
    at = datetime(2026, 11, 27, 1, 0, tzinfo=UTC)
    writer = LocalTranscriptWriter(
        tmp_path, tenant_id="acme", trace_id="trc_1", span_id="sp_1", at=at
    )
    long_text = "A" * 1500
    step0 = await writer.append(
        kind="tool_result",
        payload={
            "output": long_text,
            "header": "Authorization: Bearer super_secret_token_999",
            "api_key": "sk-testsecretkey1234567890",
            "cookie": "Cookie: sid=topsecretvalue",
        },
        ts=100.0,
    )
    assert step0.step_index == 0
    assert "output" in step0.truncated_fields
    assert writer.line_count == 1
    assert writer.compact_path.is_file() and writer.full_path.is_file()

    # 权限校验（0700 目录 / 0600 文件）
    assert stat.S_IMODE(os.stat(writer.compact_path.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(writer.full_path).st_mode) == 0o600

    full_raw = writer.full_path.read_text(encoding="utf-8")
    assert "super_secret_token_999" not in full_raw
    assert "sk-testsecretkey1234567890" not in full_raw
    assert "topsecretvalue" not in full_raw
    assert long_text in full_raw


async def test_three_tier_reader_local_remote_and_cold_object_store(tmp_path: Path) -> None:
    """三态透明读取：本节点直读 → 远端活跃节点签名热读 → 远端不可达自动回退 ObjectStore 冷归档。"""
    node_a_dir = tmp_path / "node_a"
    node_b_dir = tmp_path / "node_b"
    objects = LocalObjectStore(tmp_path / "shared_objects")
    store = MemoryTranscriptIndexStore()
    clock = [1000.0]
    signer = NodeTicketSigner(b"0123456789abcdef_cluster_key", now=lambda: clock[0])

    # Node A 落盘并归档
    at = datetime(2026, 11, 27, 2, 0, tzinfo=UTC)
    writer = LocalTranscriptWriter(
        node_a_dir, tenant_id="acme", trace_id="trc_cross", span_id="sp_a1", at=at
    )
    await writer.append(kind="user_input", payload={"text": "第 1 行输入"}, ts=1000.0)
    await writer.append(kind="tool_call", payload={"output": "Z" * 1200}, ts=1001.0)
    compact_key, full_key = await writer.archive(objects)

    rec = TranscriptIndexRecord(
        tenant_id="acme", span_id="sp_a1", trace_id="trc_cross", user_id="usr_alice",
        kind="butler", node_id="node-A", node_addr="http://node-a.internal",
        local_relpath=writer.relpath,
        compact_object_key=compact_key, full_object_key=full_key,
        byte_size=writer.byte_size, line_count=writer.line_count,
        status="completed", created_at=at, archived_at=at,
    )
    await store.upsert(rec)

    # 1) Node A 本节点直读
    reader_a = TranscriptReader(
        data_dir=node_a_dir, node_id="node-A", store=store, objects=objects, signer=signer
    )
    local_slice = await reader_a.read("acme", "sp_a1", mode="compact", start_line=1, end_line=10)
    assert local_slice.source == "local"
    assert local_slice.total_lines == 2
    assert local_slice.lines[1]["truncated_fields"] == ["output"]

    # 2) Node B 经签名热读代理读 Node A
    remote_calls: list[str] = []

    async def fake_remote_fetch(
        node_addr: str, tenant_id: str, span_id: str, mode: Any,
        start_line: int, end_line: int, ticket: str,
    ) -> TranscriptSlice:
        signer.verify(ticket, tenant_id=tenant_id, node_id="node-A", span_id=span_id)
        remote_calls.append(node_addr)
        res = await reader_a.read_local_only(
            tenant_id, span_id, mode=mode, start_line=start_line, end_line=end_line
        )
        return res.model_copy(update={"source": "remote_node"})

    reader_b = TranscriptReader(
        data_dir=node_b_dir, node_id="node-B", store=store, objects=objects,
        signer=signer, remote_fetch=fake_remote_fetch,
    )
    hot_slice = await reader_b.read("acme", "sp_a1", mode="full", start_line=2, end_line=2)
    assert hot_slice.source == "remote_node"
    assert len(hot_slice.lines) == 1
    assert hot_slice.lines[0]["payload"]["output"] == "Z" * 1200
    assert remote_calls == ["http://node-a.internal"]

    # 3) 模拟 Node A 宕机不可达 → 自动回退 ObjectStore 冷归档
    async def broken_remote(*_args: Any) -> TranscriptSlice:
        raise ConnectionError("node-A down")

    reader_b_cold = TranscriptReader(
        data_dir=node_b_dir, node_id="node-B", store=store, objects=objects,
        signer=signer, remote_fetch=broken_remote,
    )
    cold_slice = await reader_b_cold.read("acme", "sp_a1", mode="full", start_line=1, end_line=2)
    assert cold_slice.source == "object_store"
    assert cold_slice.total_lines == 2

    # 跨租户与过期票据被严格拒绝
    with pytest.raises(NotFoundError):
        await reader_b_cold.read("other_tenant", "sp_a1")
    expired_ticket = signer.sign(tenant_id="acme", node_id="node-A", span_id="sp_a1", ttl_seconds=10.0)
    clock[0] += 30.0
    with pytest.raises(AuthenticationError):
        signer.verify(expired_ticket, tenant_id="acme", node_id="node-A", span_id="sp_a1")


def test_prune_local_transcripts_by_ttl_and_size(tmp_path: Path) -> None:
    """本地热盘清理按 TTL 与容量上限淘汰最旧文件。"""
    tdir = tmp_path / "transcripts" / "acme" / "2026-11-27" / "trc_1"
    tdir.mkdir(parents=True)
    old_file = tdir / "old.full.jsonl"
    new_file = tdir / "new.full.jsonl"
    old_file.write_text("x" * 200, encoding="utf-8")
    new_file.write_text("y" * 200, encoding="utf-8")
    os.utime(old_file, (100.0, 100.0))
    os.utime(new_file, (950.0, 950.0))

    removed = prune_local_transcripts(
        tmp_path, max_age_seconds=500.0, max_total_bytes=10_000, now=1000.0
    )
    assert removed == 1
    assert not old_file.exists() and new_file.exists()


async def test_butler_turn_records_and_exposes_transcript_via_http(
    world: Any, step: Any,
) -> None:
    """端到端：管家跑完一轮后自动落盘双层 Transcript、入索引，且可经公开与内部签名路由切片查询。"""
    from dataclasses import replace

    platform = world.platform
    platform.runtimes._defaults = replace(  # noqa: SLF001
        platform.runtimes.defaults,
        execution_trace=ExecutionTraceConfig(enabled=True, tenants=("acme",)),
    )
    platform.butler_config = ButlerConfig(enabled=True, tenants=("acme",))
    await world.publish()
    await platform.identity.store.create_user(UserRecord(
        tenant_id="acme", user_id="usr_alice", display_name="Alice", created_at=1.0
    ))
    alice = Principal(role="tenant_member", tenant_id="acme", key_id=None, user_id="usr_alice")

    app = create_app(platform)
    app.dependency_overrides[caller] = lambda: alice
    world.models.script(step(text="已为您记录本次说明。"))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as http:
        resp = await http.post(
            "/v1/assistant/messages", json={"request_id": "tr_e2e_1", "text": "帮我记一下"}
        )
        assert resp.status_code == 200, resp.text
        trace_id = resp.json()["trace_id"]

        listed = (await http.get(f"/v1/traces/{trace_id}/transcripts")).json()
        assert listed["trace_id"] == trace_id
        assert len(listed["items"]) == 1
        span_id = listed["items"][0]["span_id"]
        assert listed["items"][0]["archived"] is True

        sliced = (await http.get(
            f"/v1/traces/{trace_id}/transcripts/{span_id}?mode=full&start_line=1&end_line=20"
        )).json()
        assert sliced["source"] == "local"
        assert sliced["total_lines"] >= 2
        assert any(line["kind"] == "assistant_reply" for line in sliced["lines"])

        # 内部跨节点签名只读热代理：无票据 401，持有效票据 200
        no_ticket = await http.get(
            f"/internal/nodes/local/transcripts/{span_id}?tenant_id=acme"
        )
        assert no_ticket.status_code == 401

        ticket = platform.transcript_signer.sign(
            tenant_id="acme", node_id=platform.instance_id, span_id=span_id
        )
        ok_internal = await http.get(
            f"/internal/nodes/local/transcripts/{span_id}?tenant_id=acme&mode=compact",
            headers={TICKET_HEADER: ticket},
        )
        assert ok_internal.status_code == 200
        assert ok_internal.json()["source"] == "local"
