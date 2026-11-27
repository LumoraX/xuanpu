"""真实 PostgreSQL（0001–0040 全量迁移）与进程重启持久性、4 大 L2 后端插件端到端验证测试（ADR 0027）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from scripts.l0_supervisor.migration_linter import lint_migration_sql
from server.api.app import create_app
from server.api.auth import caller
from server.core.errors import ConflictError, NotFoundError
from server.core.plugins.collections import PluginCollectionStore
from server.core.tenancy import Principal, QuotaLimits, Tenant
from server.domains.identity.models import UserRecord
from server.engine.artifacts.store import TraceArtifactRecord
from server.engine.butler.proactive_briefing import (
    AttentionBudgetGate,
    AttentionBudgetPolicy,
    ProactiveBriefingService,
)
from server.engine.butler.task_board import TaskBoardRecord, TaskBoardStep
from server.engine.butler.topic_watch import TopicWatchStore
from server.engine.forge.models import ReleaseNode
from server.engine.forge.service import ArtisanForgeService
from server.engine.tracing.models import TraceOwner
from server.engine.transcripts.models import TranscriptIndexRecord

if TYPE_CHECKING:
    from server.core.controlplane.store import ControlPlaneStore
    from server.domains.identity.store import IdentityStore


def test_migration_0040_passes_l0_expand_and_contract_linter() -> None:
    """迁移 0040_plugin_collections.sql 必须通过 L0 Expand-and-Contract 静态检查。"""
    mig = (
        Path(__file__).resolve().parents[3]
        / "server/core/controlplane/migrations/0040_plugin_collections.sql"
    )
    assert mig.is_file()
    assert lint_migration_sql(mig.name, mig.read_text(encoding="utf-8")) == []


async def test_all_new_controlplane_stores_on_memory_and_real_postgres(
    identity_stores: tuple[ControlPlaneStore, IdentityStore],
) -> None:
    """在内存与真实 PostgreSQL 上验证迁移 0036–0040 对应的全部 5 个新存储（含真实外键、隔离、乐观锁与读写回环）。"""
    store, identity_store = identity_stores
    await store.create_tenant(
        Tenant(tenant_id="acme", name="Acme", status="active", quota=QuotaLimits(), created_at=1000.0)
    )
    await identity_store.create_user(
        UserRecord(
            user_id="usr_pg_alice",
            tenant_id="acme",
            role="tenant_member",
            status="active",
            display_name="PG Alice",
            created_at=1000.0,
        )
    )
    owner = TraceOwner(tenant_id="acme", user_id="usr_pg_alice")
    now_dt = datetime.now(UTC)

    # 1. 0036: TaskBoardStore
    board = TaskBoardRecord(
        board_id="brd_pg_1",
        tenant_id="acme",
        user_id="usr_pg_alice",
        trace_id="trc_pg_1",
        title="PG 持久任务板",
        focus="验证真 PG 读写",
        status="active",
        steps=(TaskBoardStep(id="s1", title="步骤一", status="in_progress"),),
        version=1,
        created_at=now_dt,
        updated_at=now_dt,
    )
    saved_board = await store.task_board_store.upsert(owner, board)
    assert saved_board.board_id == "brd_pg_1"
    loaded_board = await store.task_board_store.get(owner, "brd_pg_1")
    assert loaded_board.title == "PG 持久任务板"
    active_boards = await store.task_board_store.list_active(owner)
    assert any(b.board_id == "brd_pg_1" for b in active_boards)

    # 2. 0037: TranscriptIndexStore
    tx_rec = TranscriptIndexRecord(
        tenant_id="acme",
        user_id="usr_pg_alice",
        trace_id="trc_pg_1",
        span_id="sp_pg_1",
        kind="butler",
        node_id="node_a",
        node_addr="http://127.0.0.1:8080",
        local_relpath="acme/2026-11-27/trc_pg_1/sp_pg_1",
        status="failed",
        error_code="timeout",
        line_count=12,
        byte_size=4096,
        created_at=now_dt,
    )
    await store.transcript_store.upsert(tx_rec)
    got_tx = await store.transcript_store.get("acme", "sp_pg_1")
    assert got_tx is not None and got_tx.error_code == "timeout"
    failed_list = await store.transcript_store.list_failed("acme", limit=10)
    assert any(r.span_id == "sp_pg_1" for r in failed_list)
    await store.transcript_store.mark_archived(
        "acme",
        "sp_pg_1",
        compact_object_key="tenants/acme/tx/sp_pg_1.compact.jsonl.gz",
        full_object_key="tenants/acme/tx/sp_pg_1.full.jsonl.gz",
        archived_at=now_dt,
    )
    archived_tx = await store.transcript_store.get("acme", "sp_pg_1")
    assert archived_tx is not None and archived_tx.compact_object_key == "tenants/acme/tx/sp_pg_1.compact.jsonl.gz"

    # 3. 0038: TraceArtifactStore
    art = TraceArtifactRecord(
        artifact_id="art_pg_1",
        tenant_id="acme",
        user_id="usr_pg_alice",
        trace_id="trc_pg_1",
        name="summary.md",
        mime_type="text/markdown",
        size=128,
        sha256="a" * 64,
        object_key="tenants/acme/artifacts/art_pg_1",
        node_id="node_a",
        contains_external_input=True,
        created_at=now_dt,
    )
    await store.trace_artifact_store.put(art)
    got_art = await store.trace_artifact_store.get(owner, "art_pg_1")
    assert got_art.name == "summary.md" and got_art.contains_external_input is True
    promoted = await store.trace_artifact_store.mark_promoted(owner, "art_pg_1", "ws_file_1")
    assert promoted.promoted_item_id == "ws_file_1"

    # 4. 0039: ReleaseNodeStore
    node1 = ReleaseNode(
        release_id="rel_pg_1",
        tenant_id="acme",
        layer="artisan",
        target_name="doc-analyst",
        version="1.0.0",
        artifact_digest="sha256:" + "1" * 64,
        generation=1,
        state="active",
        actor="usr_pg_alice",
        created_at=now_dt,
    )
    created_n1, replaced = await store.release_store.append_and_replace(node1)
    assert created_n1.generation == 1 and replaced is None
    node2 = node1.model_copy(update={"release_id": "rel_pg_2", "version": "1.1.0"})
    created_n2, replaced2 = await store.release_store.append_and_replace(node2)
    assert created_n2.generation == 2 and replaced2 is not None and replaced2.state == "retired"
    hist = await store.release_store.history("acme", "artisan", "doc-analyst")
    assert [h.generation for h in hist] == [2, 1]

    # 5. 0040: PluginCollectionBackend（含多租户隔离、乐观锁与删除）
    coll = store.plugin_collection_store
    item1 = await coll.put_item(
        tenant_id="acme",
        plugin_id="butler:topic-watch",
        user_id="usr_pg_alice",
        collection="watches",
        record_id="tw_pg_1",
        data={"topic": "AI 芯片动态"},
        expected_version=None,
        now=1000.0,
    )
    assert item1.version == 1
    item2 = await coll.put_item(
        tenant_id="acme",
        plugin_id="butler:topic-watch",
        user_id="usr_pg_alice",
        collection="watches",
        record_id="tw_pg_1",
        data={"topic": "AI 芯片动态 v2"},
        expected_version=1,
        now=1010.0,
    )
    assert item2.version == 2 and item2.data["topic"] == "AI 芯片动态 v2"
    with pytest.raises(ConflictError):
        await coll.put_item(
            tenant_id="acme",
            plugin_id="butler:topic-watch",
            user_id="usr_pg_alice",
            collection="watches",
            record_id="tw_pg_1",
            data={"topic": "冲突"},
            expected_version=1,
            now=1020.0,
        )
    # 跨租户读取必须 404
    with pytest.raises(NotFoundError):
        await coll.get_item("other_tenant", "butler:topic-watch", "usr_pg_alice", "watches", "tw_pg_1")
    listed = await coll.list_items("acme", "butler:topic-watch", "usr_pg_alice", "watches")
    assert len(listed) == 1 and listed[0].record_id == "tw_pg_1"
    assert await coll.delete_item("acme", "butler:topic-watch", "usr_pg_alice", "watches", "tw_pg_1") is True


async def test_full_restart_persistence_and_four_l2_domain_plugins(
    world: Any,
) -> None:
    """验证 4 大 L2 后端插件全部挂载运行，且所有状态（集合/追踪/注意力计数/习惯校准/Forge草稿）跨重启零丢失。"""
    platform = world.platform
    user_id = "usr_restart_alice"
    await platform.identity.store.create_user(
        UserRecord(
            user_id=user_id,
            tenant_id="acme",
            role="tenant_member",
            status="active",
            display_name="Alice",
            created_at=1000.0,
        )
    )
    who = Principal(role="tenant_member", tenant_id="acme", user_id=user_id, key_id=None)
    app = create_app(platform)
    app.dependency_overrides[caller] = lambda: who

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        # 1. 确认 4 大 L2 后端插件均已自动挂载且健康
        plugins_resp = await http.get("/v1/plugins")
        assert plugins_resp.status_code == 200, plugins_resp.text
        by_id = {p["plugin_id"]: p for p in plugins_resp.json()["items"]}
        for expected in ("custom-records", "topic-watch", "proactive-briefing", "people-graph"):
            assert expected in by_id, by_id.keys()
            assert by_id[expected]["state"] == "mounted"
            assert by_id[expected]["health"]["ok"] is True

        # 2. 通过各插件动态路由写入数据
        rec_resp = await http.post(
            "/v1/plugins/ext/custom-records/items",
            json={"data": {"k": "persisted_value"}},
        )
        assert rec_resp.status_code == 200
        rec_id = rec_resp.json()["record_id"]

        tw_resp = await http.post(
            "/v1/plugins/ext/topic-watch/create",
            json={"topic": "新能源季度财报"},
        )
        assert tw_resp.status_code == 200
        watch_id = tw_resp.json()["watch_id"]

        # 触发一次晨报投递（写入 AttentionBudgetGate 日推送计数与冷却）
        morning = datetime(2026, 11, 27, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        platform.butler.briefings._now = lambda: morning
        daily = await platform.butler.briefings.build_daily_brief(who, timezone="Asia/Shanghai", deliver=True)
        assert daily["delivered"] is True

        # 写入习惯校准（3 次忽略 + 撤回样本）
        for _ in range(3):
            ign = await http.post(
                "/v1/plugins/ext/proactive-briefing/habit/ignore",
                json={"topic_kind": "topic_watch"},
            )
            assert ign.status_code == 200
        await platform.butler.briefings.record_habit_action(who, "create_todo", undone=True)

        # 写入 Forge 草稿
        draft = await platform.forge.create_draft(
            world.admin, package_name="restart-bot", description="重启恢复验证", entry="main",
        )

        # 验证 people-graph 插件动态召回路由正常响应
        recall = await http.get("/v1/plugins/ext/people-graph/recall?query=测试")
        assert recall.status_code == 200 and "items" in recall.json()

    # 3. 模拟服务进程重启：使用同一 data_dir 全新构造内存为空的各存储与服务实例
    restarted_collections = PluginCollectionStore(
        platform.data_dir, backend=platform.store.plugin_collection_store,
    )
    restarted_rec = await restarted_collections.get(who, "custom-records", "items", rec_id)
    assert restarted_rec.data == {"k": "persisted_value"}

    restarted_watches = TopicWatchStore(platform.data_dir, collections=restarted_collections)
    watches = await restarted_watches.list(who)
    assert any(w.watch_id == watch_id and w.topic == "新能源季度财报" for w in watches)

    # 重启后的全新 AttentionBudgetGate（内存为空）在 hydrate 后必须依然记住今天的推送次数与冷却期
    fresh_gate = AttentionBudgetGate(AttentionBudgetPolicy(max_daily_pushes=1))
    restarted_briefings = ProactiveBriefingService(platform, gate=fresh_gate, now=lambda: morning + 60)
    daily_after_restart = await restarted_briefings.build_daily_brief(
        who, timezone="Asia/Shanghai", deliver=True,
    )
    assert daily_after_restart["delivered"] is False
    assert daily_after_restart["attention_decision"]["reason"] == "daily_push_cap_reached"

    # 重启后的习惯校准档案必须完整保留（连续忽略 3 次 -> topic_watch 自动静音）
    habit_after_restart = await restarted_briefings.get_habit(who)
    assert habit_after_restart.should_mute_topic("topic_watch") is True
    assert habit_after_restart.action_undo_counts["create_todo"] == (1, 1)

    # 重启后的 Forge 服务能无损恢复草稿
    restarted_forge = ArtisanForgeService(platform, platform.releases, data_dir=platform.data_dir)
    assert restarted_forge._drafts == {}
    l1 = await restarted_forge.run_l1_validate(world.admin, draft.draft_id)
    assert l1.passed is True
