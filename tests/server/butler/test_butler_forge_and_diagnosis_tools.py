"""管家内置 diagnose_trace 自排查与 forge_artisan 造匠/发布回退工具端到端测试。"""

from __future__ import annotations

from typing import Any

from server.core.tenancy import Principal
from server.domains.identity.models import UserRecord


async def test_butler_diagnose_trace_tool_reads_transcript_and_summarizes_root_cause(
    butler: dict[str, Any], step: Any,
) -> None:
    """管家可通过 diagnose_trace 在对话内读取本人历史 Transcript 并定位异常根因，且不越权看他人。"""
    world = butler["world"]

    # 第一轮：产生一条真实 Butler Transcript
    world.models.script(step(text="第一轮已完成。"))
    r1 = await butler["http"].post(
        "/v1/assistant/messages", json={"request_id": "diag_seed_1", "text": "先打个招呼"}
    )
    assert r1.status_code == 200, r1.text
    trc1 = r1.json()["trace_id"]

    # 第二轮：管家调用 diagnose_trace(list_spans) 和 summarize_root_cause
    world.models.script(
        step(calls=[
            ("d1", "diagnose_trace", {"op": "list_spans", "trace_id": trc1}),
            ("d2", "diagnose_trace", {"op": "summarize_root_cause", "trace_id": trc1}),
        ]),
        step(text="已核查该次执行记录，未发现失败步骤。"),
    )
    r2 = await butler["http"].post(
        "/v1/assistant/messages", json={"request_id": "diag_run_2", "text": f"帮我排查一下 {trc1}"}
    )
    assert r2.status_code == 200, r2.text
    assert "未发现失败步骤" in r2.json()["message"]
    # 验证第二轮工具回传确实包含 trc1 的 span 数据
    last_req_str = str(world.models.requests[-1])
    assert trc1 in last_req_str and "span_count" in last_req_str


async def test_butler_forge_artisan_tool_enforces_admin_and_full_publish_rollback(
    butler: dict[str, Any], step: Any,
) -> None:
    """非管理员调用 forge_artisan 被拒绝；租户管理员可通过管家对话完成建草稿→L-Control→门禁→审核→发版→回退。"""
    world = butler["world"]
    platform = world.platform

    # 1. 普通成员调用 forge_artisan 必须被拒绝
    world.models.script(
        step(calls=[("f0", "forge_artisan", {
            "op": "create_draft", "package_name": "denied-bot", "description": "普通成员尝试造匠"
        })]),
        step(text="你没有管理员权限，无法创建匠人。"),
    )
    r_deny = await butler["http"].post(
        "/v1/assistant/messages", json={"request_id": "forge_deny_1", "text": "帮我造个新匠人"}
    )
    assert r_deny.status_code == 200, r_deny.text
    assert "仅限租户管理员" in str(world.models.requests[-1])

    # 2. 切换为租户管理员员工身份，通过管家完整走通造匠发版
    await platform.identity.store.create_user(
        UserRecord(tenant_id="acme", user_id="usr_admin_emp", display_name="管理员", role="tenant_admin", created_at=1.0)
    )
    butler["as"]["who"] = Principal(role="tenant_admin", tenant_id="acme", user_id="usr_admin_emp", key_id=None)

    world.models.script(
        step(calls=[("f1", "forge_artisan", {
            "op": "create_draft",
            "package_name": "chat-forged",
            "description": "通过管家对话创建的匠人",
            "entry": "main",
        })]),
        step(text="草稿已建好。"),
    )
    r_create = await butler["http"].post(
        "/v1/assistant/messages", json={"request_id": "forge_adm_1", "text": "建一个 chat-forged 匠人草稿"}
    )
    assert r_create.status_code == 200, r_create.text
    # 提取建好的 draft_id
    drafts = list(platform.forge._drafts.values())
    assert len(drafts) == 1
    draft_id = drafts[0].draft_id

    # 通过管家一次串行完成：L-Control 设策略 + 添加评测样例 + 门禁校验 + 审核 + 发版
    world.models.script(
        step(calls=[("f2", "forge_artisan", {
            "op": "control_mutate",
            "draft_id": draft_id,
            "action": "set_policy",
            "params": {"sandbox_shell": False, "sandbox_network": False},
        })]),
        step(calls=[("f2b", "forge_artisan", {
            "op": "control_mutate",
            "draft_id": draft_id,
            "action": "upsert_eval",
            "params": {"name": "smoke", "entry": "main", "input": "你好", "contains": ["收到"]},
        })]),
        step(calls=[("f3", "forge_artisan", {"op": "run_gates", "draft_id": draft_id})]),
        step(calls=[("f4", "forge_artisan", {
            "op": "review", "draft_id": draft_id, "summary": "L1/L2/L3 全绿，准予发版", "approved": True,
        })]),
        step(calls=[("f5", "forge_artisan", {"op": "publish", "draft_id": draft_id})]),
        step(text="chat-forged 0.1.0 已通过门禁并发布为正式节点。"),
    )
    r_pub = await butler["http"].post(
        "/v1/assistant/messages", json={"request_id": "forge_adm_2", "text": "跑完门禁审核发版"}
    )
    assert r_pub.status_code == 200, r_pub.text
    assert "发布为正式节点" in r_pub.json()["message"]

    # 验证发布节点账本中已生成 generation=1 的活跃节点
    nodes = await platform.releases.history("acme", "artisan", "chat-forged")
    assert len(nodes) == 1 and nodes[0].generation == 1 and nodes[0].state == "active"
