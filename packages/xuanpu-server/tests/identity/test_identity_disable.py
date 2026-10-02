"""员工停用流水线（设计 7.2）：标停用、删登录会话、依次通知「员工停用」监听者。

P1 登记的监听者只有一个：关闭他名下全部 active 会话（``identity.listeners``，以系统调用者
``system:user-disabled`` 调 ``SessionManager.close``）。经真实的 ASGI 应用验证停用之后的效果：登录
cookie 401、代调用 403、会话被关；只做前两步时，主人已停用的会话发消息 403。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, get_args

import pytest
from prometheus_client.parser import text_string_to_metric_families
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.controlplane.models import AuditQuery, SessionRecord
from xuanpu_server.errors import ConflictError
from xuanpu_server.identity import DisabledReason
from xuanpu_server.identity.cookies import login_token_hash
from xuanpu_server.identity.disable import FEISHU_EVENT_ACTOR
from xuanpu_server.identity.listeners import (
    CLEANUP_ACTOR,
    CloseUserSessions,
    SessionCleanupError,
)
from xuanpu_server.metrics import DISABLE_REASONS
from xuanpu_server.tenancy import Principal, Tenant, system_principal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from xuanpu_server.controlplane.models import AuditRecord

WRITE = {"X-Requested-By": "xuanpu"}


def bearer(key: str) -> dict[str, str]:
    """带 API key 的请求头。"""
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
async def ident(identity_world: Any) -> AsyncIterator[Any]:
    """配了 ``public_url`` 的平台，样例业务包已发布并生效。"""
    async with identity_world() as item:
        await item.world.publish()
        yield item


async def create(ident: Any, headers: dict[str, str]) -> str:
    """以给定身份新建会话，返回会话标识。"""
    response = await ident.http.post(
        "/v1/sessions", headers=headers, json={"package": "support-desk"}
    )
    assert response.status_code == 201, response.text
    session_id: str = response.json()["session_id"]
    return session_id


async def status_of(ident: Any, session_id: str) -> str:
    """会话当前的状态（以管理员 key 取）。"""
    response = await ident.http.get(
        f"/v1/sessions/{session_id}", headers=bearer(ident.world.admin_key)
    )
    status: str = response.json()["status"]
    return status


async def records(ident: Any, kind: str, **conditions: Any) -> list[AuditRecord]:
    """租户 acme 里某种审计记录。"""
    found: list[AuditRecord] = await ident.world.platform.store.query_audit(
        "acme", AuditQuery(kind=kind, **conditions)
    )
    return found


def disabled_counts(ident: Any) -> dict[str, float]:
    """``xuanpu_users_disabled_total`` 各个 ``reason`` 的计数。"""
    text = ident.world.platform.metrics.render().decode()
    return {
        sample.labels["reason"]: sample.value
        for family in text_string_to_metric_families(text)
        if family.name == "xuanpu_users_disabled"
        for sample in family.samples
    }


class Recorder:
    """记下每次被通知的监听者。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def on_user_disabled(self, tenant_id: str, user_id: str) -> None:
        """记一笔。"""
        self.calls.append((tenant_id, user_id))


class Broken:
    """总是失败的监听者；异常文本里有不该进审计的内容。"""

    async def on_user_disabled(self, tenant_id: str, user_id: str) -> None:
        """失败。"""
        raise RuntimeError("连不上 https://internal.example/secret-path")


# --- 三步 ---


async def test_disable_runs_the_three_steps(ident: Any) -> None:
    """停用：标停用并记 ``user_disabled``；删掉他的全部登录会话（cookie 401、代调用 403）；关掉
    他名下    全部 active 会话（经浏览器与代调用建的都算），别人的会话不动。"""
    identity = ident.world.platform.identity
    alice, bob = await ident.user(name="爱丽丝"), await ident.user(name="鲍勃")
    tokens = [await ident.login(alice), await ident.login(alice)]
    system = await ident.system_key()
    as_alice = {"Authorization": f"Bearer {system}", "X-Xuanpu-Act-As": alice.user_id}
    hers = [
        await create(ident, {**ident.cookie(tokens[0]), **WRITE}),
        await create(ident, as_alice),
    ]
    bobs = await create(ident, {**ident.cookie(await ident.login(bob)), **WRITE})
    admin = ident.world.admin

    outcome = await identity.disable_user("acme", alice.user_id, "admin", admin, keep_an_admin=True)

    assert (outcome.changed, outcome.cleanup_failed) == (True, ())
    assert (outcome.user.status, outcome.user.disabled_reason) == ("disabled", "admin")
    assert outcome.user.disabled_at is not None
    for token in tokens:
        response = await ident.http.get("/v1/me", headers=ident.cookie(token))
        assert response.status_code == 401
    assert (await ident.http.get("/v1/me", headers=as_alice)).status_code == 403
    assert [await status_of(ident, session_id) for session_id in hers] == ["closed", "closed"]
    assert await status_of(ident, bobs) == "active"

    [record] = await records(ident, "user_disabled")
    assert (record.actor, record.user_id, record.data) == (
        admin.actor,
        alice.user_id,
        {"reason": "admin"},
    )
    for session_id in hers:
        [closed] = await records(ident, "session_closed", session_id=session_id)
        assert (closed.actor, closed.user_id) == (f"system:{CLEANUP_ACTOR}", alice.user_id)
    assert disabled_counts(ident) == {
        "feishu_resigned": 0.0,
        "feishu_frozen": 0.0,
        "feishu_exited": 0.0,
        "admin": 1.0,
    }


async def test_disable_again_reruns_listeners_without_a_second_record(ident: Any) -> None:
    """再停用一次：状态不变（原因、时间保持第一次的），不再记 ``user_disabled``、不再计数，但全部
    监听者重跑一遍。"""
    identity = ident.world.platform.identity
    recorder = Recorder()
    identity.disabling.register("recorder", recorder)
    alice = await ident.user(name="爱丽丝")
    feishu = system_principal("acme", FEISHU_EVENT_ACTOR)

    first = await identity.disable_user(
        "acme", alice.user_id, "feishu_resigned", feishu, keep_an_admin=False
    )
    second = await identity.disable_user(
        "acme", alice.user_id, "admin", ident.world.admin, keep_an_admin=True
    )

    assert (first.changed, second.changed) == (True, False)
    assert second.user.disabled_reason == "feishu_resigned"
    assert second.user.disabled_at == first.user.disabled_at
    assert recorder.calls == [("acme", alice.user_id)] * 2
    [record] = await records(ident, "user_disabled")
    assert (record.actor, record.data) == ("system:feishu-event", {"reason": "feishu_resigned"})
    assert disabled_counts(ident)["feishu_resigned"] == 1.0
    assert disabled_counts(ident)["admin"] == 0.0


async def test_failing_listener_is_audited_and_does_not_stop_the_rest(ident: Any) -> None:
    """某个监听者失败：记 ``user_cleanup_failed``（监听者名与异常类型，不带异常文本），其余照常跑；
    再停用一次会重跑它。"""
    identity = ident.world.platform.identity
    recorder = Recorder()
    identity.disabling.register("broken", Broken())
    identity.disabling.register("recorder", recorder)
    alice = await ident.user(name="爱丽丝")
    session_id = await create(ident, {**ident.cookie(await ident.login(alice)), **WRITE})
    admin = ident.world.admin

    outcome = await identity.disable_user("acme", alice.user_id, "admin", admin, keep_an_admin=True)

    assert outcome.cleanup_failed == ("broken",)
    assert recorder.calls == [("acme", alice.user_id)]
    assert await status_of(ident, session_id) == "closed"
    [failed] = await records(ident, "user_cleanup_failed")
    assert (failed.actor, failed.user_id) == (admin.actor, alice.user_id)
    assert failed.data == {"listener": "broken", "error": "RuntimeError"}

    again = await identity.disable_user("acme", alice.user_id, "admin", admin, keep_an_admin=True)
    assert again.cleanup_failed == ("broken",)
    assert len(await records(ident, "user_cleanup_failed")) == 2
    assert len(recorder.calls) == 2


async def test_listener_names_are_unique(ident: Any) -> None:
    """同一个名字不能登记两次（审计里按名字区分监听者）。"""
    pipeline = ident.world.platform.identity.disabling
    with pytest.raises(ValueError, match="close_sessions"):
        pipeline.register("close_sessions", Recorder())


async def test_enable_only_changes_status(ident: Any) -> None:
    """重新启用：只改状态、记 ``user_enabled``（带停用时的原因），不恢复已关闭的会话，也不恢复已
    删掉的登录会话；已是启用的再启用一次不记。"""
    identity = ident.world.platform.identity
    alice = await ident.user(name="爱丽丝")
    token = await ident.login(alice)
    session_id = await create(ident, {**ident.cookie(token), **WRITE})
    admin = ident.world.admin
    await identity.disable_user("acme", alice.user_id, "admin", admin, keep_an_admin=True)

    enabled = await identity.enable_user("acme", alice.user_id, admin)
    again = await identity.enable_user("acme", alice.user_id, admin)

    assert (enabled.status, enabled.disabled_reason, enabled.disabled_at) == ("active", None, None)
    assert again.status == "active"
    [record] = await records(ident, "user_enabled")
    assert (record.actor, record.user_id) == (admin.actor, alice.user_id)
    assert record.data == {"previous_reason": "admin"}
    assert await status_of(ident, session_id) == "closed"
    assert (await ident.http.get("/v1/me", headers=ident.cookie(token))).status_code == 401
    fresh = await ident.login(alice)
    assert (await ident.http.get("/v1/me", headers=ident.cookie(fresh))).status_code == 200


async def test_keep_an_admin_guards_the_last_admin(ident: Any) -> None:
    """``keep_an_admin`` 为真时不停用最后一名启用的管理员（409）；为假时照样停用（离职的管理员必须
    停用，兜底靠平台管理员重新指定）。"""
    identity = ident.world.platform.identity
    boss = await ident.user(role="tenant_admin", name="老板")
    with pytest.raises(ConflictError, match="最后一名"):
        await identity.disable_user(
            "acme", boss.user_id, "admin", ident.world.admin, keep_an_admin=True
        )
    assert (await ident.store.get_user("acme", boss.user_id)).status == "active"
    assert await records(ident, "user_disabled") == []

    feishu = system_principal("acme", FEISHU_EVENT_ACTOR)
    outcome = await identity.disable_user(
        "acme", boss.user_id, "feishu_resigned", feishu, keep_an_admin=False
    )
    assert outcome.changed


async def test_audit_is_written_after_the_effects(ident: Any, monkeypatch: Any) -> None:
    """``user_disabled`` 排在删登录会话之后：它写不进去时操作报错，但停用与删登录会话都已落下；
    重试时状态已是停用，这条记录不会补写（已知限制，写在审计手册里），指标照计。"""
    identity = ident.world.platform.identity
    alice = await ident.user(name="爱丽丝")
    token = await ident.login(alice)
    audit = ident.world.platform.audit
    original = audit.control

    async def failing(principal: Any, tenant_id: str, kind: str, *args: Any, **kwargs: Any) -> None:
        """``user_disabled`` 写不进去，别的照常。"""
        if kind == "user_disabled":
            raise ConnectionError("审计库挂了")
        await original(principal, tenant_id, kind, *args, **kwargs)

    monkeypatch.setattr(audit, "control", failing)
    admin = ident.world.admin
    with pytest.raises(ConnectionError):
        await identity.disable_user("acme", alice.user_id, "admin", admin, keep_an_admin=True)
    assert (await ident.store.get_user("acme", alice.user_id)).status == "disabled"
    session = await ident.store.get_login_session(login_token_hash(token), now=time.time())
    assert session is None

    monkeypatch.undo()
    again = await identity.disable_user("acme", alice.user_id, "admin", admin, keep_an_admin=True)
    assert not again.changed
    assert await records(ident, "user_disabled") == []
    assert disabled_counts(ident)["admin"] == 1.0


# --- 只做前两步：主人已停用的运行时规则兜底 ---


async def test_owner_disabled_rule_covers_unfinished_cleanup(ident: Any, step: Any) -> None:
    """只做了前两步（第 3 步还在后台、或崩溃时丢了）：会话还开着，但主人已停用，任何调用者发消息
    都是 403；第 3 步跑完之后会话已关闭，发消息是 409。"""
    identity = ident.world.platform.identity
    alice = await ident.user(name="爱丽丝")
    session_id = await create(ident, {**ident.cookie(await ident.login(alice)), **WRITE})
    admin = ident.world.admin

    marked = await identity.disabling.mark_disabled(
        "acme", alice.user_id, "admin", admin, keep_an_admin=True
    )
    assert marked.changed
    assert await status_of(ident, session_id) == "active"
    url = f"/v1/sessions/{session_id}/messages"
    blocked = await ident.http.post(url, headers=bearer(ident.world.admin_key), json={"text": "hi"})
    assert (blocked.status_code, blocked.json()["error"]["code"]) == (403, "session_owner_disabled")

    assert await identity.disabling.notify("acme", alice.user_id, admin) == ()
    assert await status_of(ident, session_id) == "closed"
    closed = await ident.http.post(url, headers=bearer(ident.world.admin_key), json={"text": "hi"})
    assert (closed.status_code, closed.json()["error"]["code"]) == (409, "conflict")


# --- 会话被别的实例持有 ---


async def test_session_held_by_another_instance_is_marked_closed(
    ident: Any, step: Any, read_turn: Any, tmp_path: Any
) -> None:
    """会话正由别的实例持有：照样在控制面标成 closed（不需要持有租约），立即生效——持有它的实例也
    不再接受新消息（409）；那个实例上的引擎不受通知，按空闲回收或停机时释放。"""
    world = ident.world
    alice = await ident.user(name="爱丽丝")
    session_id = await create(ident, {**ident.cookie(await ident.login(alice)), **WRITE})
    other = world.sibling("inst-other", data_dir=(tmp_path / "data-other").resolve())
    try:
        world.models.script(step(text="在另一个实例上"))
        await read_turn(await other.sessions.send_message(world.admin, session_id, "你好"))
        held = await world.platform.store.get_session("acme", session_id)
        assert held.owner == "inst-other"

        await world.platform.identity.disable_user(
            "acme", alice.user_id, "admin", world.admin, keep_an_admin=True
        )

        record = await world.platform.store.get_session("acme", session_id)
        assert (record.status, record.owner) == ("closed", "inst-other")
        assert other.sessions.held_sessions() == 1
        await world.platform.identity.enable_user("acme", alice.user_id, world.admin)
        with pytest.raises(ConflictError, match="已关闭"):
            await other.sessions.send_message(world.admin, session_id, "还在吗")
    finally:
        await other.close()


# --- 关闭会话的监听者（单元） ---


class FakeCloser:
    """替身的会话管理：关会话就是把控制面记录标 closed。``fail`` 里的报错；``silent`` 里的不报错，
    却原样返回仍是 active 的记录。"""

    def __init__(
        self, store: InMemoryControlPlane, fail: set[str], silent: set[str] | None = None
    ) -> None:
        self.store = store
        self.fail = fail
        self.silent = silent or set()
        self.calls: list[tuple[Principal, str]] = []

    async def close(self, principal: Principal, session_id: str) -> SessionRecord:
        """关一个会话。"""
        self.calls.append((principal, session_id))
        if session_id in self.fail:
            raise RuntimeError("关不掉")
        if session_id in self.silent:
            record = await self.store.get_session("acme", session_id)
            assert record is not None
            return record
        return await self.store.update_session("acme", session_id, at=time.time(), status="closed")


async def test_close_listener_pages_through_and_reports_failures() -> None:
    """控制面列会话没有游标：反复取「仍是 active 的前 N 个」，关不掉的每轮多取同样多个，保证没试过
    的都轮得到；每个只试一次。关不掉的（``close`` 报错，或返回了却仍是 active）最后一并报
    ``SessionCleanupError``，关掉的不回滚。以系统调用者关：``tenant_admin``、只在本租户、``actor`` 为
    ``system:user-disabled``。"""
    store = InMemoryControlPlane()
    await store.create_tenant(Tenant(tenant_id="acme", name="Acme"))
    mine = [f"ses_{index}" for index in range(7)]
    for index, session_id in enumerate([*mine, "ses_other"]):
        await store.create_session(
            SessionRecord(
                session_id=session_id,
                tenant_id="acme",
                package="p",
                version="1",
                entry="e",
                created_at=float(index),
                updated_at=float(index),
                created_by="user:usr_x",
                user_id="usr_other" if session_id == "ses_other" else "usr_alice",
            )
        )
    closer = FakeCloser(store, fail={"ses_6", "ses_5"}, silent={"ses_4"})
    listener = CloseUserSessions(store=store, sessions=closer, page_size=2)

    with pytest.raises(SessionCleanupError, match="3 个"):
        await listener.on_user_disabled("acme", "usr_alice")

    attempted = [session_id for _, session_id in closer.calls]
    assert sorted(attempted) == sorted(mine)
    stuck = closer.fail | closer.silent
    for session_id in mine:
        record = await store.get_session("acme", session_id)
        assert record is not None
        assert record.status == ("active" if session_id in stuck else "closed")
    other = await store.get_session("acme", "ses_other")
    assert other is not None and other.status == "active"
    principals = {principal for principal, _ in closer.calls}
    assert principals == {system_principal("acme", CLEANUP_ACTOR)}
    [principal] = principals
    assert (principal.role, principal.tenant_id, principal.actor) == (
        "tenant_admin",
        "acme",
        "system:user-disabled",
    )

    # 重跑：只剩上次关不掉的三个，这次都关掉
    closer.fail.clear()
    closer.silent.clear()
    await listener.on_user_disabled("acme", "usr_alice")
    rerun = sorted(session_id for _, session_id in closer.calls[len(attempted) :])
    assert rerun == ["ses_4", "ses_5", "ses_6"]
    active = await store.list_sessions("acme", status="active", user_id="usr_alice")
    assert active == []


# --- 系统调用者与指标 ---


def test_system_principal_shape() -> None:
    """系统调用者：只属于一个租户，不带 key 与员工，``actor`` 为 ``system:<名字>``。"""
    principal = system_principal("acme", "feishu-event")
    assert (principal.actor, principal.key_id, principal.user_id) == (
        "system:feishu-event",
        None,
        None,
    )
    with pytest.raises(ValueError, match="租户"):
        Principal(role="tenant_admin", tenant_id=None, key_id=None, system="x-y")
    with pytest.raises(ValueError, match="key"):
        Principal(role="tenant_admin", tenant_id="acme", key_id="k1", system="x-y")
    with pytest.raises(ValueError, match="名字"):
        system_principal("acme", "Bad Name")


def test_disable_reason_labels_match_the_model() -> None:
    """指标 ``xuanpu_users_disabled_total`` 的 ``reason`` 与停用原因的取值一致。"""
    assert get_args(DisabledReason) == DISABLE_REASONS
