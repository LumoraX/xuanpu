"""会话的归属与可见性（设计 8.1），以及主人停用之后的运行时规则（设计 7.2 末段）。

经真实的 ASGI 应用走请求：员工用登录 cookie（夹具 ``identity_world`` 直接经身份存储建登录会话，
见本目录的 ``conftest.py``），系统代员工调用用签发时带 ``act_as_users`` 的 key。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import pytest
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.controlplane.models import AuditQuery, SessionRecord
from xuanpu_server.errors import PlatformError
from xuanpu_server.identity import InMemoryIdentityStore
from xuanpu_server.runtime.ownership import SessionOwnership
from xuanpu_server.tenancy import Principal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx
    from xuanpu_server.identity import UserRecord

WRITE = {"X-Requested-By": "xuanpu"}
MISSING = "ses_" + "0" * 24
"""一个不存在的会话标识（形状与平台分配的一样）。"""


def bearer(key: str) -> dict[str, str]:
    """带 API key 的请求头。"""
    return {"Authorization": f"Bearer {key}"}


def act_as(key: str, user: UserRecord) -> dict[str, str]:
    """用 API key 代某名员工调用的请求头。"""
    return {**bearer(key), "X-Xuanpu-Act-As": user.user_id}


def not_found(session_id: str) -> dict[str, Any]:
    """会话不存在时的响应体。"""
    return {"error": {"code": "not_found", "message": f"会话 {session_id!r} 不存在"}}


@pytest.fixture
async def ident(identity_world: Any) -> AsyncIterator[Any]:
    """配了 ``public_url`` 的平台，样例业务包已发布并生效。"""
    async with identity_world() as item:
        await item.world.publish()
        yield item


async def cookie_of(ident: Any, user: UserRecord) -> dict[str, str]:
    """员工浏览器登录的请求头（写请求要的自定义头一并带上，读请求带着也无妨）。"""
    return {**ident.cookie(await ident.login(user)), **WRITE}


async def create(ident: Any, headers: dict[str, str]) -> dict[str, Any]:
    """以给定身份新建会话，返回会话对象。"""
    response = await ident.http.post(
        "/v1/sessions", headers=headers, json={"package": "support-desk"}
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def listed(ident: Any, headers: dict[str, str], **params: str) -> set[str]:
    """以给定身份列出的会话标识。"""
    response = await ident.http.get("/v1/sessions", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return {item["session_id"] for item in response.json()["sessions"]}


async def call(
    ident: Any, headers: dict[str, str], operation: tuple[str, str, Any], session_id: str
) -> httpx.Response:
    """对某个会话做一次操作：``(方法, 路径后缀, 请求体)``。"""
    method, suffix, body = operation
    url = f"/v1/sessions/{session_id}{suffix}"
    kwargs: dict[str, Any] = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    response: httpx.Response = await ident.http.request(method, url, **kwargs)
    return response


# --- 会话的主人 ---


async def test_session_records_its_owner(ident: Any) -> None:
    """建会话时记下调用者代表的员工：浏览器登录、代员工调用都是这名员工；API key 直接建的为空。"""
    alice = await ident.user(name="爱丽丝")
    system = await ident.system_key()
    by_cookie = await create(ident, await cookie_of(ident, alice))
    by_act_as = await create(ident, act_as(system, alice))
    by_key = await create(ident, bearer(ident.world.member_key))
    assert by_cookie["user_id"] == alice.user_id
    assert by_act_as["user_id"] == alice.user_id
    assert by_key["user_id"] is None
    fetched = await ident.http.get(
        f"/v1/sessions/{by_cookie['session_id']}", headers=bearer(ident.world.admin_key)
    )
    assert fetched.json()["user_id"] == alice.user_id


# --- 可见范围 ---


async def test_member_lists_only_own_sessions(ident: Any) -> None:
    """普通成员只列出自己的会话：``mine`` 被忽略（``mine=false`` 也只列自己的，不报错）。"""
    alice, bob = await ident.user(name="爱丽丝"), await ident.user(name="鲍勃")
    alice_headers = await cookie_of(ident, alice)
    mine = {(await create(ident, alice_headers))["session_id"] for _ in range(2)}
    await create(ident, await cookie_of(ident, bob))
    await create(ident, bearer(ident.world.member_key))

    assert await listed(ident, alice_headers) == mine
    assert await listed(ident, alice_headers, mine="false") == mine
    assert await listed(ident, alice_headers, mine="true") == mine
    assert await listed(ident, alice_headers, status="active") == mine
    assert await listed(ident, alice_headers, status="closed") == set()


OPERATIONS = [
    pytest.param(("GET", "", None), id="get"),
    pytest.param(("DELETE", "", None), id="close"),
    pytest.param(("POST", "/messages", {"text": "你好"}), id="message"),
    pytest.param(("POST", "/resume", {"resolutions": {"req_x": {"granted": True}}}), id="resume"),
    pytest.param(("POST", "/cancel", None), id="cancel"),
    pytest.param(("GET", "/events?submission_id=sub_x", None), id="replay"),
    pytest.param(("GET", "/pending", None), id="pending"),
]


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_member_is_blocked_from_others_sessions(
    ident: Any, operation: tuple[str, str, Any]
) -> None:
    """普通成员碰别人的会话（取、关、发消息、送审批、停止、回放、待办）：与会话不存在同一个 404。"""
    alice, bob = await ident.user(name="爱丽丝"), await ident.user(name="鲍勃")
    alice_headers = await cookie_of(ident, alice)
    theirs = (await create(ident, await cookie_of(ident, bob)))["session_id"]
    by_key = (await create(ident, bearer(ident.world.member_key)))["session_id"]
    requests_before = len(ident.world.models.requests)

    for session_id in (theirs, by_key, MISSING):
        response = await call(ident, alice_headers, operation, session_id)
        assert (response.status_code, response.json()) == (404, not_found(session_id))

    # 别人的会话没有被动过：还开着，没有发出消息，也没有采样
    admin = bearer(ident.world.admin_key)
    for session_id in (theirs, by_key):
        response = await ident.http.get(f"/v1/sessions/{session_id}", headers=admin)
        assert response.json()["status"] == "active"
    sent = await ident.world.platform.store.query_audit("acme", AuditQuery(kind="message_sent"))
    assert sent == []
    assert len(ident.world.models.requests) == requests_before


async def test_admins_and_plain_keys_see_every_session(ident: Any) -> None:
    """浏览器登录的管理员、不代员工的 API key（不论角色）看本租户全部会话。"""
    alice, admin = await ident.user(name="爱丽丝"), await ident.user(role="tenant_admin")
    alice_session = (await create(ident, await cookie_of(ident, alice)))["session_id"]
    everyone = {
        alice_session,
        (await create(ident, await cookie_of(ident, admin)))["session_id"],
        (await create(ident, bearer(ident.world.member_key)))["session_id"],
    }
    for headers in (
        await cookie_of(ident, admin),
        bearer(ident.world.admin_key),
        bearer(ident.world.member_key),
    ):
        assert await listed(ident, headers) == everyone
        response = await ident.http.get(f"/v1/sessions/{alice_session}", headers=headers)
        assert response.status_code == 200


async def test_mine_filters_by_user_or_by_creator(ident: Any) -> None:
    """``mine=true``：代表员工的按员工（浏览器与代调用建的都算），不代员工的 key 按创建者。"""
    admin = await ident.user(role="tenant_admin")
    system = await ident.system_key()
    admin_cookie = await cookie_of(ident, admin)
    as_admin = act_as(system, admin)
    by_admin = {
        (await create(ident, admin_cookie))["session_id"],
        (await create(ident, as_admin))["session_id"],
    }
    by_admin_key = (await create(ident, bearer(ident.world.admin_key)))["session_id"]
    by_member_key = (await create(ident, bearer(ident.world.member_key)))["session_id"]

    assert await listed(ident, admin_cookie, mine="true") == by_admin
    assert await listed(ident, as_admin, mine="true") == by_admin
    assert await listed(ident, bearer(ident.world.admin_key), mine="true") == {by_admin_key}
    assert await listed(ident, bearer(ident.world.member_key), mine="true") == {by_member_key}
    assert await listed(ident, admin_cookie) == by_admin | {by_admin_key, by_member_key}


async def test_acting_as_a_member_sees_what_the_member_sees(ident: Any) -> None:
    """代员工调用按员工的可见范围生效：管理员的 key 代成员调用只看这名成员的；成员的 key 代管理员
    调用，生效角色是成员，也只看这名管理员自己的。"""
    alice, bob = await ident.user(name="爱丽丝"), await ident.user(name="鲍勃")
    admin = await ident.user(role="tenant_admin")
    alice_own = (await create(ident, await cookie_of(ident, alice)))["session_id"]
    bobs = (await create(ident, await cookie_of(ident, bob)))["session_id"]
    admins = (await create(ident, await cookie_of(ident, admin)))["session_id"]

    as_alice = act_as(await ident.system_key(role="tenant_admin"), alice)
    created = (await create(ident, as_alice))["session_id"]
    assert await listed(ident, as_alice) == {alice_own, created}
    response = await ident.http.get(f"/v1/sessions/{bobs}", headers=as_alice)
    assert (response.status_code, response.json()) == (404, not_found(bobs))
    assert await listed(ident, await cookie_of(ident, alice)) == {alice_own, created}

    as_admin = act_as(await ident.system_key(role="tenant_member"), admin)
    assert await listed(ident, as_admin) == {admins}
    response = await ident.http.get(f"/v1/sessions/{alice_own}", headers=as_admin)
    assert response.status_code == 404


# --- 主人停用 ---


async def suspended_session(ident: Any, step: Any, user: UserRecord) -> tuple[str, str]:
    """员工开一个会话，让模型发起一次要审批的写操作、停在审批点；返回 ``(会话, 待办标识)``。"""
    headers = await cookie_of(ident, user)
    session_id = (await create(ident, headers))["session_id"]
    ident.world.models.script(
        step(calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]), step(text="好了")
    )
    response = await ident.http.post(
        f"/v1/sessions/{session_id}/messages", headers=headers, json={"text": "改工单"}
    )
    assert response.status_code == 200
    assert "event: turn_suspended" in response.text
    pending = await ident.http.get(f"/v1/sessions/{session_id}/pending", headers=headers)
    [item] = pending.json()["pending"]
    return session_id, item["request_id"]


async def disable(ident: Any, user: UserRecord) -> None:
    """直接经身份存储停用员工（停用流水线是后续任务）。"""
    assert await ident.store.set_user_status(
        "acme", user.user_id, "disabled", reason="admin", at=time.time()
    )


async def test_disabled_owner_blocks_messages_and_resume(ident: Any, step: Any) -> None:
    """主人停用之后，任何调用者（包括管理员与系统 key）都不能再发消息、送审批结果：
    ``403 session_owner_disabled``，什么也不提交；停止生成、关闭会话放行，只读的照常。"""
    alice, admin = await ident.user(name="爱丽丝"), await ident.user(role="tenant_admin")
    session_id, request_id = await suspended_session(ident, step, alice)
    await disable(ident, alice)
    requests_before = len(ident.world.models.requests)

    system = await ident.system_key()
    callers = [bearer(ident.world.admin_key), await cookie_of(ident, admin), act_as(system, admin)]
    for headers in callers:
        for operation in (
            ("POST", "/messages", {"text": "继续"}),
            ("POST", "/resume", {"resolutions": {request_id: {"granted": True}}}),
        ):
            response = await call(ident, headers, operation, session_id)
            assert response.status_code == 404, response.text
            assert response.json()["error"]["code"] == "not_found"
    assert len(ident.world.models.requests) == requests_before
    assert ident.world.calls() == []
    kinds = {"message_sent", "suspension_resolved_by"}
    records = await ident.world.platform.store.query_audit(
        "acme", AuditQuery(session_id=session_id, source="control")
    )
    assert [record.kind for record in records if record.kind in kinds] == ["message_sent"]

    admin_key = bearer(ident.world.admin_key)
    pending = await call(ident, admin_key, ("GET", "/pending", None), session_id)
    assert pending.status_code == 404
    stopped = await call(ident, admin_key, ("POST", "/cancel", None), session_id)
    assert (stopped.status_code, stopped.json()) == (200, {"cancelled": []})
    closed = await call(ident, admin_key, ("DELETE", "", None), session_id)
    assert (closed.status_code, closed.json()["status"]) == (200, "closed")


async def test_owner_status_is_read_on_every_request(ident: Any, step: Any) -> None:
    """主人状态每次请求都查身份存储，不缓存：停用立即拦住，重新启用立即放行。"""
    alice = await ident.user(name="爱丽丝")
    session_id = (await create(ident, await cookie_of(ident, alice)))["session_id"]
    admin_key = act_as(await ident.system_key(), alice)
    message = ("POST", "/messages", {"text": "你好"})
    ident.world.models.script(step(text="第一句"), step(text="第二句"))
    assert (await call(ident, admin_key, message, session_id)).status_code == 200

    await disable(ident, alice)
    assert (await call(ident, admin_key, message, session_id)).status_code == 403

    assert await ident.store.set_user_status("acme", alice.user_id, "active", at=time.time())
    response = await call(ident, admin_key, message, session_id)
    assert response.status_code == 200
    assert "第二句" in response.text


async def test_missing_owner_record_is_an_internal_error(caplog: Any) -> None:
    """会话的主人在身份存储里查不到（员工记录不会删除，是存储出了问题）：按内部错误拒绝、记错误
    日志，不当作启用放行。"""
    store = InMemoryControlPlane()
    record = SessionRecord(
        session_id="ses_1",
        tenant_id="acme",
        package="support-desk",
        version="1.0.0",
        entry="triage",
        created_at=0.0,
        updated_at=0.0,
        created_by="user:usr_ghost",
        user_id="usr_ghost",
    )
    await store.create_session(record)
    ownership = SessionOwnership(store, InMemoryIdentityStore(store))
    admin = Principal(role="tenant_admin", tenant_id="acme", key_id="k1")
    assert await ownership.visible(admin, "ses_1") == record
    with (
        caplog.at_level(logging.ERROR, logger="xuanpu_server.runtime.ownership"),
        pytest.raises(PlatformError) as caught,
    ):
        await ownership.writable(Principal("tenant_member", "acme", None, user_id="usr_ghost"), "ses_1")
    assert (caught.value.code, caught.value.status) == ("internal_error", 500)
    assert "usr_ghost" in caplog.text
