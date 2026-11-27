"""P1 评审闭环测试：本地 Transcript 定时裁剪、主动简报后台巡检、Grant 收敛 API、TopicWatch 落盘 API 与会诊跳过 SOUL。"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from server.engine.butler.models import ArtisanRef, DispatchRecord
from server.engine.butler.topic_watch import TopicWatchStore
from server.engine.tracing.config import ExecutionTraceConfig
from server.engine.tracing.models import (
    AttemptUsage,
    SpanFact,
    TraceOwner,
)
from server.engine.tracing.retention import TraceRetention
from server.engine.transcripts.writer import LocalTranscriptWriter


async def test_trace_retention_prunes_expired_local_transcripts(
    butler: dict[str, Any],
) -> None:
    """TraceRetention.run_once() 自动联动裁剪超过 TTL 的本地热盘 Transcript，保留新文件。"""
    platform = butler["world"].platform
    now = 1_800_000_000.0
    old_ts = now - 20 * 86400.0
    fresh_ts = now - 2 * 86400.0

    old_writer = LocalTranscriptWriter(
        platform.data_dir,
        tenant_id="acme",
        trace_id="trc_old",
        span_id="sp_old",
        at=datetime.fromtimestamp(old_ts, UTC),
    )
    await old_writer.append(kind="user_input", payload={"text": "旧日志"}, ts=old_ts)
    os.utime(old_writer.compact_path, (old_ts, old_ts))
    os.utime(old_writer.full_path, (old_ts, old_ts))

    fresh_writer = LocalTranscriptWriter(
        platform.data_dir,
        tenant_id="acme",
        trace_id="trc_fresh",
        span_id="sp_fresh",
        at=datetime.fromtimestamp(fresh_ts, UTC),
    )
    await fresh_writer.append(kind="user_input", payload={"text": "新日志"}, ts=fresh_ts)
    os.utime(fresh_writer.compact_path, (fresh_ts, fresh_ts))
    os.utime(fresh_writer.full_path, (fresh_ts, fresh_ts))

    retention = TraceRetention(
        platform.store.trace_store,
        ExecutionTraceConfig(enabled=True, tenants=("acme",)),
        now=lambda: now,
        data_dir=platform.data_dir,
        transcript_ttl_days=14,
    )
    await retention.run_once()

    assert not old_writer.compact_path.exists()
    assert not old_writer.full_path.exists()
    assert fresh_writer.compact_path.exists()
    assert fresh_writer.full_path.exists()


async def test_proactive_briefing_scan_once_delivers_pre_meeting_and_daily(
    butler: dict[str, Any],
) -> None:
    """ProactiveBriefingService.scan_once() 接入后台巡检后，能按本地早间窗口自动推送会前速览与晨报。"""
    platform = butler["world"].platform
    alice = butler["as"]["who"]
    morning = datetime(2026, 11, 27, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

    # 固定简报时钟为上午 08:30，并创建一个 15 分钟后开始的日程
    platform.butler.briefings._now = lambda: morning
    await platform.workspace.create(
        alice,
        "event",
        {
            "title": "Q4 预算评审",
            "start_at": morning + 900,
            "end_at": morning + 2700,
            "timezone": "Asia/Shanghai",
        },
        "req_brief_ev1",
    )

    assert platform.proactive_worker._briefings_scan is not None
    delivered = await platform.butler.briefings.scan_once()
    assert delivered >= 2

    # 再次立即巡检受注意力冷却与幂等去重保护，不再重复投递
    delivered_again = await platform.butler.briefings.scan_once()
    assert delivered_again == 0


async def test_converge_grant_http_endpoint(butler: dict[str, Any]) -> None:
    """POST /v1/assistant/traces/{trace_id}/converge-grant 从真实 Trace 与派工收敛最小权限 Grant。"""
    platform = butler["world"].platform
    owner = TraceOwner(tenant_id="acme", user_id=butler["as"]["who"].user_id)
    now_dt = datetime.now(UTC)

    trace_id = "trc_grant_http_1"
    await platform.store.trace_store.open_trace(
        owner, trace_id=trace_id, origin="user", title="试运行收敛测试", at=now_dt,
    )
    await platform.store.trace_store.apply(
        owner,
        trace_id,
        (
            SpanFact(
                seq=1,
                span_id="sp_root",
                at=now_dt,
                kind="butler",
                name="butler",
                state="completed",
                attempt_id="att_1",
                usage=AttemptUsage(
                    attempt_id="att_1",
                    input_tokens=12000,
                    output_tokens=4000,
                    usage_complete=True,
                ),
            ),
            SpanFact(
                seq=2,
                span_id="tl.read_personal",
                parent_span_id="sp_root",
                at=now_dt,
                kind="tool",
                name="read_personal",
                state="completed",
            ),
        ),
    )
    await platform.store.dispatch_store.create(
        owner,
        DispatchRecord(
            dispatch_id="dsp_grant_1",
            tenant_id="acme",
            user_id=owner.user_id,
            trace_id=trace_id,
            parent_span_id="sp_root",
            request_id="req_grant_1",
            input_fingerprint="a" * 64,
            artisan=ArtisanRef(
                package="doc-analyst",
                version="1.2.0",
                digest="sha256:" + "b" * 64,
                entry="analyze",
                decision_ref="catalog:acme",
            ),
            host="task",
            instruction="分析报表",
            state="completed",
            created_at=now_dt,
            updated_at=now_dt,
        ),
        max_per_trace=10,
        max_active_traces=10,
    )

    resp = await butler["http"].post(
        f"/v1/assistant/traces/{trace_id}/converge-grant",
        json={"budget_ceiling": 500000},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["source_trace_id"] == trace_id
    assert data["artisan_refs"] == ["doc-analyst@1.2.0:analyze"]
    assert data["allowed_tools"] == ["read_personal"]
    assert data["observed_tokens"] == 16000
    assert data["recommended_max_tokens"] == 24000
    assert data["require_approval_on_unlisted"] is True


async def test_topic_watches_http_crud_persistence_and_silent_eval(
    butler: dict[str, Any],
) -> None:
    """TopicWatch HTTP 路由支持落盘重启持久、无变化绝对静默、有新信号放行并更新基线。"""
    platform = butler["world"].platform
    http = butler["http"]

    # 1. 创建追踪主题
    created_resp = await http.post("/v1/assistant/topic-watches", json={"topic": "竞品 A 动态"})
    assert created_resp.status_code == 200, created_resp.text
    watch_id = created_resp.json()["watch_id"]

    # 2. 模拟重启重建 TopicWatchStore，验证仍能读到已登记主题
    restarted_store = TopicWatchStore(platform.data_dir)
    items = await restarted_store.list(butler["as"]["who"])
    assert any(i.watch_id == watch_id and i.topic == "竞品 A 动态" for i in items)

    # 3. 首次新信号放行并写入基线
    # 避免测试恰好在夜间静音时段运行，将 gate 策略放宽为无静音窗口
    platform.butler.briefings.gate.policy = platform.butler.briefings.gate.policy.model_copy(
        update={"quiet_start_hour": 0, "quiet_end_hour": 0}
    )
    ev1 = await http.post(
        f"/v1/assistant/topic-watches/{watch_id}/evaluate",
        json={"signal": "竞品 A 发布 v3.0 降价 20%", "deliver": True},
    )
    assert ev1.status_code == 200, ev1.text
    body1 = ev1.json()
    assert body1["notify"] is True
    assert body1["reason"] == "new_signal"
    assert "竞品 A 发布 v3.0" in (body1["message"] or "")

    # 4. 相同信号（仅空白差异）必须保持绝对静默
    ev2 = await http.post(
        f"/v1/assistant/topic-watches/{watch_id}/evaluate",
        json={"signal": "  竞品 A   发布 v3.0 降价 20% \n"},
    )
    assert ev2.status_code == 200, ev2.text
    assert ev2.json()["notify"] is False
    assert ev2.json()["reason"] == "unchanged_signal"

    # 5. 列表与删除
    listed = await http.get("/v1/assistant/topic-watches")
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1

    deleted = await http.delete(f"/v1/assistant/topic-watches/{watch_id}")
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True


async def test_consult_compare_pool_omits_butler_soul_layer(
    butler: dict[str, Any],
) -> None:
    """普通管家轮注入静态 SOUL.md；会诊客观比对轮（include_soul=False）使用独立池且不注入 SOUL。"""
    engines = butler["world"].platform.butler.engines
    normal_pool = await engines.pool("acme", include_soul=True)
    compare_pool = await engines.pool("acme", include_soul=False)
    assert normal_pool is not compare_pool
    assert ("acme", True) in engines._pools
    assert ("acme", False) in engines._pools
    # 校验两个引擎池的 instruction_layers 配置确实不同（普通轮含 butler-soul，比对轮不含）
    normal_layers = getattr(normal_pool, "_instruction_layers", None) or []
    compare_layers = getattr(compare_pool, "_instruction_layers", None) or []
    assert any(getattr(layer, "name", "") == "butler-soul" for layer in normal_layers)
    assert not any(getattr(layer, "name", "") == "butler-soul" for layer in compare_layers)
