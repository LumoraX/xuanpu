"""纯个人空间「人物与关系」领域、HTTP 接口、头像安全与管家确认卡测试。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest

from server.api import create_app
from server.core.controlplane import AuditQuery, InMemoryControlPlane
from server.core.tenancy import Principal
from server.domains.identity import LoginSession, UserRecord
from server.domains.identity.config import IdentityConfig
from server.domains.identity.cookies import login_cookie_name, new_login_token
from server.domains.identity.service import IdentitySettings
from server.domains.people.routing import resolve_people
from server.engine.assistant.proposals import resolve_native_choice
from server.engine.assistant.service import Choice

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 32


@pytest.fixture
async def env(world_opener, local_sandboxes):
    """准备两名真实登录员工与可调用的 HTTP 测试客户端。"""
    settings = IdentitySettings.from_config("https://xuanpu.example.com", IdentityConfig())
    store = InMemoryControlPlane()
    world = await world_opener(
        store=store, sandboxes=local_sandboxes(), identity_settings=settings
    )
    await world.platform.reminders.close()
    users: dict[str, dict[str, str]] = {}
    principals: dict[str, Principal] = {}
    for user_id in ("usr_a", "usr_b"):
        await world.platform.identity.store.create_user(
            UserRecord(tenant_id="acme", user_id=user_id, display_name=user_id, created_at=time.time())
        )
        token, hashed = new_login_token()
        now = time.time()
        await world.platform.identity.store.create_login_session(
            LoginSession(
                token_hash=hashed,
                tenant_id="acme",
                user_id=user_id,
                created_at=now,
                expires_at=now + 3600,
                last_seen_at=now,
            )
        )
        users[user_id] = {"Cookie": f"{login_cookie_name(settings.cookie_secure)}={token}"}
        principals[user_id] = Principal(key_id=None, role="member", tenant_id="acme", user_id=user_id)
    app = create_app(world.platform)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://xuanpu.example.com"
    ) as client:
        try:
            yield SimpleNamespace(world=world, store=store, client=client, users=users, principals=principals)
        finally:
            await world.platform.close()


def hdr(env, user: str = "usr_a") -> dict[str, str]:
    """附带真实同源 CSRF 头的员工请求头。"""
    return {**env.users[user], "X-Requested-By": "xuanpu", "Origin": "https://xuanpu.example.com"}


async def test_person_crud_birth_search_isolation_and_audit(env) -> None:
    """名片 CRUD、不完整出生信息、跨用户/机器隔离、幂等与脱敏审计。"""
    client = env.client
    # 机器 API key 拒绝访问个人人物空间
    machine = await client.get(
        "/v1/people", headers={"Authorization": "Bearer " + env.world.admin_key}
    )
    assert machine.status_code == 403

    # 公历 2 月 30 日应被严格校验拒绝
    bad_birth = await client.post(
        "/v1/people",
        headers=hdr(env),
        json={
            "request_id": "bad-birth",
            "profile": {"name": "非法日期", "birth": {"year": 1990, "month": 2, "day": 30}},
        },
    )
    assert bad_birth.status_code == 400

    # 新建含不完整出生年份、多联系方式、别名与扩展字段的人物
    profile = {
        "name": "王建国",
        "aliases": ["王总", "Kevin"],
        "gender": "male",
        "birth": {"year": 1986, "month": 5, "day": 12, "calendar": "solar"},
        "organization": "星辰科技",
        "title": "供应链副总裁",
        "city": "杭州",
        "contacts": [
            {"kind": "phone", "value": "13800138000", "label": "工作", "primary": True},
            {"kind": "wechat", "value": "wx_wang", "label": "常用"},
        ],
        "tags": ["供应链", "潜在客户"],
        "summary": "务实高效，李四介绍认识",
        "extra": {"alma_mater": "浙江大学", "preferences": ["龙井", "不吃辣"]},
    }
    created = await client.post(
        "/v1/people", headers=hdr(env), json={"request_id": "create-wang", "profile": profile}
    )
    assert created.status_code == 201, created.text
    wang = created.json()
    assert wang["name"] == "王建国"
    assert wang["birth_year"] == 1986
    assert wang["birth_month"] == 5
    assert wang["has_avatar"] is False

    # 同 request_id 同体幂等重放；异体 409
    replayed = await client.post(
        "/v1/people", headers=hdr(env), json={"request_id": "create-wang", "profile": profile}
    )
    assert replayed.status_code == 201
    assert replayed.json()["id"] == wang["id"]

    conflict = await client.post(
        "/v1/people",
        headers=hdr(env),
        json={"request_id": "create-wang", "profile": {"name": "另一个人"}},
    )
    assert conflict.status_code == 409

    # 检索：按别名、扩展字段、生日月份都能命中
    assert len((await client.get("/v1/people?q=Kevin", headers=env.users["usr_a"])).json()["items"]) == 1
    assert len((await client.get("/v1/people?q=浙江大学", headers=env.users["usr_a"])).json()["items"]) == 1
    assert len((await client.get("/v1/people?birth_month=5", headers=env.users["usr_a"])).json()["items"]) == 1
    assert len((await client.get("/v1/people?birth_month=6", headers=env.users["usr_a"])).json()["items"]) == 0

    # 另一位员工完全隔离不可见
    assert (await client.get("/v1/people", headers=env.users["usr_b"])).json()["items"] == []
    assert (await client.get(f"/v1/people/{wang['id']}", headers=env.users["usr_b"])).status_code == 404

    # 审计记录严格脱敏：不含姓名、手机号、微信号、摘要
    audits = await env.store.query_audit("acme", AuditQuery())
    people_audits = [a for a in audits if a.kind.startswith("people.")]
    assert people_audits
    blob = str([a.data for a in people_audits])
    for secret in ("王建国", "13800138000", "wx_wang", "浙江大学"):
        assert secret not in blob


async def test_avatar_upload_security_and_photo_attachments(env) -> None:
    """头像魔数校验（拒绝 SVG）、对象存储读写、跨用户隔离及个人文件照片引用。"""
    client = env.client
    created = await client.post(
        "/v1/people", headers=hdr(env), json={"request_id": "p-av", "profile": {"name": "赵敏"}}
    )
    person = created.json()
    pid = person["id"]

    # 拒绝 SVG 防止 XSS
    svg = await client.put(
        f"/v1/people/{pid}/avatar?version={person['version']}",
        headers={**hdr(env), "Content-Type": "image/svg+xml"},
        content=b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
    )
    assert svg.status_code == 400

    # 上传合法 PNG 头像
    uploaded = await client.put(
        f"/v1/people/{pid}/avatar?version={person['version']}",
        headers={**hdr(env), "Content-Type": "image/png"},
        content=PNG_HEADER,
    )
    assert uploaded.status_code == 200, uploaded.text
    updated = uploaded.json()
    assert updated["has_avatar"] is True
    assert "object_key" not in str(updated)

    # 本人受权读取头像且带安全响应头
    got = await client.get(f"/v1/people/{pid}/avatar", headers=env.users["usr_a"])
    assert got.status_code == 200
    assert got.content == PNG_HEADER
    assert got.headers["content-type"].startswith("image/png")
    assert got.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in got.headers["cache-control"]

    # 他人禁止读取头像
    denied = await client.get(f"/v1/people/{pid}/avatar", headers=env.users["usr_b"])
    assert denied.status_code == 404

    # 个人工作台上传名片原图并挂到人物与互动记录
    up = await client.post(
        "/v1/workspace/files?name=card.jpg&request_id=card-1",
        headers={**hdr(env), "Content-Type": "image/jpeg"},
        content=JPEG_HEADER,
    )
    assert up.status_code == 200, up.text
    file_id = up.json()["id"]

    note = await client.post(
        "/v1/people/notes",
        headers=hdr(env),
        json={
            "request_id": "n-card",
            "data": {"content": "展会交换名片原图", "person_ids": [pid], "attachment_file_ids": [file_id]},
        },
    )
    assert note.status_code == 201, note.text
    assert note.json()["attachment_file_ids"] == [file_id]

    # 清除头像
    cleared = await client.delete(
        f"/v1/people/{pid}/avatar?version={updated['version']}", headers=hdr(env)
    )
    assert cleared.status_code == 200
    assert cleared.json()["has_avatar"] is False
    assert (await client.get(f"/v1/people/{pid}/avatar", headers=env.users["usr_a"])).status_code == 404


async def test_relations_graph_matter_link_merge_unmerge_and_trash(env) -> None:
    """对称关系防重、事项关联反查、2 跳图展开、可逆软合并与批量回收恢复。"""
    client = env.client
    # 创建 3 个人与 1 个个人事项（项目）
    p1 = (await client.post("/v1/people", headers=hdr(env), json={"request_id": "r-p1", "profile": {"name": "张三", "tags": ["技术"]}})).json()
    p2 = (await client.post("/v1/people", headers=hdr(env), json={"request_id": "r-p2", "profile": {"name": "李四", "organization": "星辰科技"}})).json()
    p3 = (await client.post("/v1/people", headers=hdr(env), json={"request_id": "r-p3", "profile": {"name": "王五"}})).json()
    matter = (
        await client.post(
            "/v1/workspace/items",
            headers=hdr(env),
            json={"kind": "matter", "data": {"title": "供应链升级项目"}, "request_id": "m-1"},
        )
    ).json()

    # 对称关系：张三 <-> 李四 为同事，反向再建应报 409 防重
    rel1 = await client.post(
        "/v1/people/relations",
        headers=hdr(env),
        json={
            "request_id": "rel-1",
            "data": {"from_person_id": p1["id"], "target_kind": "person", "target_id": p2["id"], "relation_type": "colleague"},
        },
    )
    assert rel1.status_code == 201, rel1.text
    assert rel1.json()["symmetric"] is True

    dup = await client.post(
        "/v1/people/relations",
        headers=hdr(env),
        json={
            "request_id": "rel-dup",
            "data": {"from_person_id": p2["id"], "target_kind": "person", "target_id": p1["id"], "relation_type": "colleague"},
        },
    )
    assert dup.status_code == 409

    # 李四与王五共同参与同一事项
    for idx, person in enumerate((p2, p3), start=1):
        res = await client.post(
            "/v1/people/relations",
            headers=hdr(env),
            json={
                "request_id": f"rel-m-{idx}",
                "data": {
                    "from_person_id": person["id"],
                    "target_kind": "matter",
                    "target_id": matter["id"],
                    "relation_type": "matter_role",
                    "role": "负责人" if idx == 1 else "评审",
                },
            },
        )
        assert res.status_code == 201

    # 按事项反查人物
    matter_people = (await client.get(f"/v1/workspace/items/{matter['id']}/people", headers=env.users["usr_a"])).json()["items"]
    assert {item["person"]["name"] for item in matter_people} == {"李四", "王五"}

    # 从张三出发 2 跳展开：张三 -> 李四 -> 事项 -> 王五
    graph = (await client.get(f"/v1/people/{p1['id']}/graph?depth=2", headers=env.users["usr_a"])).json()
    node_ids = {n["id"] for n in graph["nodes"]}
    assert {p1["id"], p2["id"], p3["id"], matter["id"]}.issubset(node_ids)

    # 给 p1 传头像，然后软合并 p1 -> p2（p2 应自动继承头像与别名“张三”）
    p1 = (
        await client.put(
            f"/v1/people/{p1['id']}/avatar?version={p1['version']}",
            headers={**hdr(env), "Content-Type": "image/png"},
            content=PNG_HEADER,
        )
    ).json()
    merged = await client.post(
        f"/v1/people/{p1['id']}/merge",
        headers=hdr(env),
        json={
            "target_id": p2["id"],
            "source_version": p1["version"],
            "target_version": p2["version"],
            "request_id": "merge-1",
        },
    )
    assert merged.status_code == 200, merged.text
    m_src, m_tgt = merged.json()["source"], merged.json()["target"]
    assert m_src["merged_into_id"] == p2["id"]
    assert m_tgt["has_avatar"] is True
    assert "张三" in m_tgt["profile"]["aliases"]

    # 撤销合并：双方恢复独立且原头像归还
    unmerged = await client.post(
        f"/v1/people/{p1['id']}/unmerge",
        headers=hdr(env),
        json={
            "source_version": m_src["version"],
            "target_version": m_tgt["version"],
            "request_id": "unmerge-1",
        },
    )
    assert unmerged.status_code == 200, unmerged.text
    u_src, u_tgt = unmerged.json()["source"], unmerged.json()["target"]
    assert u_src["merged_into_id"] is None
    assert u_tgt["has_avatar"] is False

    # 回收站批量回收与恢复
    trashed = (await client.delete(f"/v1/people/{p1['id']}?version={u_src['version']}", headers=hdr(env))).json()
    assert trashed["trashed_at"] is not None
    restored = (await client.post(f"/v1/people/{p1['id']}/restore?version={trashed['version']}", headers=hdr(env))).json()
    assert restored["trashed_at"] is None
    detail = (await client.get(f"/v1/people/{p1['id']}", headers=env.users["usr_a"])).json()
    assert len(detail["relations"]) == 1


async def test_butler_proposal_resolution_and_confirm_bundle(env) -> None:
    """管家提案解析（附重名候选）与持久确认卡原子落库。"""
    client = env.client
    platform = env.world.platform
    who = env.principals["usr_a"]

    # 先建一个已有联系人“老王”与个人事项
    existing = await platform.people.create_person(who, {"name": "老王", "aliases": ["王建国"]}, "seed-lao-wang")
    matter = await platform.workspace.create(who, "matter", {"title": "华东拓展"}, "seed-matter")

    # 管家调用 @people/record 提出复合草稿，自动附带重名候选
    draft = {
        "persons": [
            {"action": "create", "temp_ref": "p1", "profile": {"name": "王建国", "birth": {"year": 1986}}},
            {"action": "create", "temp_ref": "p2", "profile": {"name": "陈总", "organization": "云帆"}},
        ],
        "relations": [
            {"from_ref": "p2", "target_kind": "person", "target_ref": "p1", "relation_type": "introduced_by"},
            {"from_ref": "p1", "target_kind": "matter", "target_ref": matter["id"], "relation_type": "matter_role", "role": "顾问"},
        ],
        "note": {"content": "陈总介绍了王建国参与华东拓展", "person_refs": ["p1", "p2"], "matter_id": matter["id"]},
    }
    outcome = await resolve_native_choice(
        platform,
        who,
        Choice(package="@people", entry="record", message=None, people_draft=draft),
        now=time.time(),
    )
    assert outcome is not None and outcome.people_draft is not None
    assert outcome.people_draft["persons"][0]["similar_candidates"][0]["person_id"] == existing["id"]

    # 模拟将提案写入主对话历史并经 /v1/assistant/proposals/{id}/confirm 确认
    req_id = "prop-people-1"
    await platform.workspace.append_message(
        who, "assistant", "请确认人物名片", req_id + ":assistant", metadata={"route": outcome.saved()}
    )
    pending = (await client.get("/v1/assistant/proposals", headers=env.users["usr_a"])).json()["items"]
    assert any(item["proposal_id"] == req_id for item in pending)

    confirmed = await client.post(
        f"/v1/assistant/proposals/{req_id}/confirm", headers=hdr(env), json={"data": draft}
    )
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["state"] == "confirmed"
    assert len(body["result"]["persons"]) == 2
    assert len(body["result"]["relations"]) == 2
    assert body["result"]["note"] is not None

    # 确认后不再出现在待确认列表，重复确认幂等返回同一结果
    pending_after = (await client.get("/v1/assistant/proposals", headers=env.users["usr_a"])).json()["items"]
    assert not any(item["proposal_id"] == req_id for item in pending_after)
    again = await client.post(
        f"/v1/assistant/proposals/{req_id}/confirm", headers=hdr(env), json={"data": draft}
    )
    assert again.status_code == 200
    assert again.json()["result"] == body["result"]

    # @people/manage 打开管理入口不带草稿
    manage = await resolve_people(platform, who, "manage", None)
    assert manage.open_people is True
    assert manage.people_draft is None

    # 管家只读工具 read_personal 支持 people_search / person_detail / matter_people
    from server.engine.butler.turn import ButlerTurn

    turn = ButlerTurn(
        who=who,
        request_id="read-1",
        trace_id="tr_1",
        timezone="Asia/Shanghai",
        user_text="查王建国",
    )
    tools = platform.butler.tools
    res_search = await tools._read_personal(turn, {"kind": "people_search", "query": "王建国"}, "c1")
    assert not res_search.is_error and "王建国" in res_search.output
    created_wang_id = body["result"]["persons"][0]["id"]
    res_detail = await tools._read_personal(
        turn, {"kind": "person_detail", "person_id": created_wang_id}, "c2"
    )
    assert not res_detail.is_error and "陈总" in res_detail.output
    res_matter = await tools._read_personal(
        turn, {"kind": "matter_people", "matter_id": matter["id"]}, "c3"
    )
    assert not res_matter.is_error and "顾问" in res_matter.output

    # 清空当前主对话上下文：后续主对话返回空，但已保存的人物记录不受影响
    cleared = await client.post("/v1/workspace/messages/clear", headers=hdr(env))
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["cleared_after_id"] >= 1
    assert (await client.get("/v1/workspace/messages?latest=true", headers=env.users["usr_a"])).json()["items"] == []
    assert len((await client.get("/v1/people?q=王建国", headers=env.users["usr_a"])).json()["items"]) >= 1

    # 执行树工具节点按本人归属反查审计详情（arguments / output），他人 404
    from datetime import UTC, datetime

    from server.core.controlplane.models import AuditRecord
    from server.engine.tracing.models import SpanFact, TraceOwner

    now_dt = datetime.now(UTC)
    now_ts = now_dt.timestamp()
    trace_owner = TraceOwner(tenant_id="acme", user_id="usr_a")
    call_id = "call_test_people_1"
    await env.store.trace_store.open_trace(
        trace_owner, trace_id="trc_audit_test", origin="user", title="查人物", at=now_dt
    )
    await env.store.trace_store.apply(
        trace_owner,
        "trc_audit_test",
        [
            SpanFact(seq=1, span_id="ex.1", at=now_dt, kind="butler", name="butler", state="running"),
            SpanFact(
                seq=2,
                span_id="tl." + call_id,
                at=now_dt,
                kind="tool",
                name="read_personal",
                parent_span_id="ex.1",
                state="completed",
            ),
        ],
        source="butler",
    )
    await env.store.append_audit([
        AuditRecord(
            tenant_id="acme",
            user_id="usr_a",
            source="engine",
            kind="tool_call_started",
            at=now_ts,
            actor="engine",
            data={"call_id": call_id, "name": "read_personal", "arguments": '{"kind":"people_search"}'},
        ),
        AuditRecord(
            tenant_id="acme",
            user_id="usr_a",
            source="engine",
            kind="tool_call_completed",
            at=now_ts + 0.01,
            actor="engine",
            data={"call_id": call_id, "name": "read_personal", "output": '{"items":[{"name":"王建国"}]}', "is_error": False, "duration_ms": 10},
        ),
    ])
    span_res = await client.get(
        f"/v1/traces/trc_audit_test/spans/tl.{call_id}/audit", headers=env.users["usr_a"]
    )
    assert span_res.status_code == 200, span_res.text
    sbody = span_res.json()
    assert sbody["call_id"] == call_id
    assert "people_search" in sbody["arguments"]
    assert "王建国" in sbody["output"]
    assert sbody["duration_ms"] == 10
    assert (
        await client.get(f"/v1/traces/trc_audit_test/spans/tl.{call_id}/audit", headers=env.users["usr_b"])
    ).status_code == 404
