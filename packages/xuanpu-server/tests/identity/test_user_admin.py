"""用户管理接口：列员工、取员工、改角色、停用与启用（设计第 9 节）。

列、改只对租户管理员开放；取员工是管理员或本人（别人的与不存在的同一个 404）。改角色、停用都保住
最后一名启用的管理员（``409 conflict``）。停用走停用流水线（原因 ``admin``），启用只改状态。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from xuanpu_server.controlplane.models import AuditQuery

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx
    from xuanpu_server.controlplane.models import AuditRecord
    from xuanpu_server.identity import UserRecord

WRITE = {"X-Requested-By": "xuanpu"}
USER_FIELDS = {
    "user_id",
    "display_name",
    "avatar_url",
    "role",
    "status",
    "disabled_reason",
    "created_at",
    "last_login_at",
    "disabled_at",
    "unverified_since",
}
"""员工对外表示的全部字段：没有外部身份、登录会话之类的东西。"""


def bearer(key: str) -> dict[str, str]:
    """带 API key 的请求头。"""
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
async def ident(identity_world: Any) -> AsyncIterator[Any]:
    """配了 ``public_url`` 的平台，样例业务包已发布并生效。"""
    async with identity_world() as item:
        await item.world.publish()
        yield item


async def cookie_of(ident: Any, user: UserRecord) -> dict[str, str]:
    """员工浏览器登录的请求头（带上写请求要的自定义头）。"""
    return {**ident.cookie(await ident.login(user)), **WRITE}


def admin(ident: Any) -> dict[str, str]:
    """租户管理员 API key 的请求头。"""
    return bearer(ident.world.admin_key)


async def records(ident: Any, kind: str) -> list[AuditRecord]:
    """租户 acme 里某种审计记录。"""
    found: list[AuditRecord] = await ident.world.platform.store.query_audit(
        "acme", AuditQuery(kind=kind)
    )
    return found


async def put_role(ident: Any, headers: dict[str, str], user_id: str, role: str) -> httpx.Response:
    """改角色。"""
    response: httpx.Response = await ident.http.put(
        f"/v1/users/{user_id}/role", headers=headers, json={"role": role}
    )
    return response


async def put_status(
    ident: Any, headers: dict[str, str], user_id: str, status: str
) -> httpx.Response:
    """改状态。"""
    response: httpx.Response = await ident.http.put(
        f"/v1/users/{user_id}/status", headers=headers, json={"status": status}
    )
    return response


# --- 列与取 ---


async def test_list_users_filters_and_pages(ident: Any) -> None:
    """管理员列员工：按 ``user_id`` 升序，可按状态、角色、查不到标记过滤；``after`` 与 ``limit``
    翻页，    满页时 ``next_after`` 是这一页最后一名的标识。"""
    users = [await ident.user(name=f"员工{index}") for index in range(3)]
    boss = await ident.user(role="tenant_admin", name="老板")
    await ident.world.platform.identity.disable_user(
        "acme", users[0].user_id, "admin", ident.world.admin, keep_an_admin=True
    )
    await ident.store.mark_user_unverified("acme", users[1].user_id, at=1.0)
    ordered = sorted(user.user_id for user in [*users, boss])

    response = await ident.http.get("/v1/users", headers=admin(ident))
    assert response.status_code == 200
    body = response.json()
    assert [item["user_id"] for item in body["users"]] == ordered
    assert body["next_after"] is None
    assert all(set(item) == USER_FIELDS for item in body["users"])

    async def ids(**params: Any) -> list[str]:
        """按条件列出的员工标识。"""
        found = await ident.http.get("/v1/users", headers=admin(ident), params=params)
        assert found.status_code == 200, found.text
        return [item["user_id"] for item in found.json()["users"]]

    assert await ids(status="disabled") == [users[0].user_id]
    assert await ids(role="tenant_admin") == [boss.user_id]
    assert await ids(unverified="true") == [users[1].user_id]
    assert len(await ids(unverified="false")) == 3
    first = await ident.http.get("/v1/users", headers=admin(ident), params={"limit": 2})
    assert first.json()["next_after"] == ordered[1]
    assert await ids(after=ordered[1], limit=2) == ordered[2:]
    bad = await ident.http.get("/v1/users", headers=admin(ident), params={"status": "gone"})
    assert bad.status_code == 400


async def test_get_user_is_for_admins_or_oneself(ident: Any) -> None:
    """取员工：管理员取谁都行；成员只能取自己（浏览器登录或被代调用都算），取别人的与不存在的同一个
    404。"""
    alice, bob = await ident.user(name="爱丽丝"), await ident.user(name="鲍勃")
    alice_headers = await cookie_of(ident, alice)
    system = await ident.system_key()
    as_alice = {**bearer(system), "X-Xuanpu-Act-As": alice.user_id}

    own = await ident.http.get(f"/v1/users/{alice.user_id}", headers=alice_headers)
    assert own.status_code == 200
    assert (own.json()["user_id"], own.json()["display_name"]) == (alice.user_id, "爱丽丝")
    assert set(own.json()) == USER_FIELDS
    acted = await ident.http.get(f"/v1/users/{alice.user_id}", headers=as_alice)
    assert acted.status_code == 200
    by_admin = await ident.http.get(f"/v1/users/{bob.user_id}", headers=admin(ident))
    assert by_admin.status_code == 200

    missing = "usr_" + "0" * 24
    others = await ident.http.get(f"/v1/users/{bob.user_id}", headers=alice_headers)
    absent = await ident.http.get(f"/v1/users/{missing}", headers=alice_headers)
    assert (others.status_code, absent.status_code) == (404, 404)
    assert others.json()["error"]["message"] == f"用户 {bob.user_id!r} 不存在"
    assert absent.json()["error"]["message"] == f"用户 {missing!r} 不存在"


async def test_members_cannot_manage_users(ident: Any) -> None:
    """成员（API key、浏览器登录）不能列员工、改角色、改状态：403。"""
    alice = await ident.user(name="爱丽丝")
    for headers in (bearer(ident.world.member_key), await cookie_of(ident, alice)):
        listed = await ident.http.get("/v1/users", headers=headers)
        assert listed.status_code == 403
        assert (await put_role(ident, headers, alice.user_id, "tenant_admin")).status_code == 403
        assert (await put_status(ident, headers, alice.user_id, "disabled")).status_code == 403
    assert (await ident.store.get_user("acme", alice.user_id)).role == "tenant_member"


# --- 改角色 ---


async def test_set_role_records_before_and_after(ident: Any) -> None:
    """改角色：记 ``user_role_changed``（前后值；``user_id`` 是被改的员工，``actor`` 是调用者）；
    改成    原来的角色不记。成员升为管理员之后马上能做管理员的事。"""
    alice = await ident.user(name="爱丽丝")
    response = await put_role(ident, admin(ident), alice.user_id, "tenant_admin")
    assert response.status_code == 200
    assert response.json()["role"] == "tenant_admin"
    same = await put_role(ident, admin(ident), alice.user_id, "tenant_admin")
    assert same.status_code == 200

    [record] = await records(ident, "user_role_changed")
    assert (record.actor, record.user_id) == (ident.world.admin.actor, alice.user_id)
    assert record.data == {"before": "tenant_member", "after": "tenant_admin"}
    listed = await ident.http.get("/v1/users", headers=await cookie_of(ident, alice))
    assert listed.status_code == 200
    invalid = await put_role(ident, admin(ident), alice.user_id, "platform_admin")
    assert invalid.status_code == 400
    missing = await put_role(ident, admin(ident), "usr_" + "0" * 24, "tenant_admin")
    assert missing.status_code == 404


async def test_last_admin_cannot_be_demoted_or_disabled(ident: Any) -> None:
    """最后一名启用的管理员：降级、停用都是 ``409 conflict`` 并说明原因，什么也不改、不记审计。"""
    boss = await ident.user(role="tenant_admin", name="老板")
    boss_headers = await cookie_of(ident, boss)
    for response in (
        await put_role(ident, admin(ident), boss.user_id, "tenant_member"),
        await put_status(ident, boss_headers, boss.user_id, "disabled"),
    ):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"
        assert "最后一名启用的管理员" in response.json()["error"]["message"]
    user = await ident.store.get_user("acme", boss.user_id)
    assert (user.role, user.status) == ("tenant_admin", "active")
    assert await records(ident, "user_role_changed") == []
    assert await records(ident, "user_disabled") == []


async def test_concurrent_demotions_leave_an_admin(ident: Any) -> None:
    """两名管理员同时被降级：至多一个成功，另一个 409；租户里总剩一名启用的管理员。"""
    first = await ident.user(role="tenant_admin", name="甲")
    second = await ident.user(role="tenant_admin", name="乙")
    responses = await asyncio.gather(
        put_role(ident, admin(ident), first.user_id, "tenant_member"),
        put_role(ident, admin(ident), second.user_id, "tenant_member"),
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    admins = await ident.http.get(
        "/v1/users", headers=admin(ident), params={"role": "tenant_admin", "status": "active"}
    )
    assert len(admins.json()["users"]) == 1


# --- 改状态 ---


async def test_disable_and_enable_through_the_api(ident: Any) -> None:
    """停用走流水线（原因 ``admin``：会话被关、登录失效），响应带 ``cleanup_failed``；启用只改状态；
    状态取值不对是 400，员工不存在是 404。"""
    alice = await ident.user(name="爱丽丝")
    alice_headers = await cookie_of(ident, alice)
    session = await ident.http.post(
        "/v1/sessions", headers=alice_headers, json={"package": "support-desk"}
    )
    session_id = session.json()["session_id"]

    disabled = await put_status(ident, admin(ident), alice.user_id, "disabled")
    assert disabled.status_code == 200
    body = disabled.json()
    assert (body["status"], body["disabled_reason"], body["cleanup_failed"]) == (
        "disabled",
        "admin",
        [],
    )
    assert (await ident.http.get("/v1/me", headers=alice_headers)).status_code == 401
    closed = await ident.http.get(f"/v1/sessions/{session_id}", headers=admin(ident))
    assert closed.json()["status"] == "closed"
    [record] = await records(ident, "user_disabled")
    assert record.data == {"reason": "admin"}

    enabled = await put_status(ident, admin(ident), alice.user_id, "active")
    assert enabled.status_code == 200
    assert (enabled.json()["status"], enabled.json()["cleanup_failed"]) == ("active", [])
    assert len(await records(ident, "user_enabled")) == 1
    reopened = await ident.http.get(f"/v1/sessions/{session_id}", headers=admin(ident))
    assert reopened.json()["status"] == "closed"

    assert (await put_status(ident, admin(ident), alice.user_id, "gone")).status_code == 400
    missing = await put_status(ident, admin(ident), "usr_" + "0" * 24, "disabled")
    assert missing.status_code == 404


async def test_admin_may_disable_another_admin(ident: Any) -> None:
    """不是最后一名就可以停用管理员，包括管理员停用自己。"""
    first = await ident.user(role="tenant_admin", name="甲")
    second = await ident.user(role="tenant_admin", name="乙")
    first_headers = await cookie_of(ident, first)
    response = await put_status(ident, first_headers, second.user_id, "disabled")
    assert response.status_code == 200
    blocked = await put_status(ident, first_headers, first.user_id, "disabled")
    assert blocked.status_code == 409
    third = await ident.user(role="tenant_admin", name="丙")
    own = await put_status(ident, first_headers, first.user_id, "disabled")
    assert own.status_code == 200
    assert (await ident.http.get("/v1/me", headers=first_headers)).status_code == 401
    assert (await ident.store.get_user("acme", third.user_id)).status == "active"
