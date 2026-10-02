"""按员工的用量（设计 8.3）：一轮结束入账时，会话有主人的同时累加按员工的表（被停下的一轮按估算
计入的也算）；``GET /v1/usage/users`` 按天、按员工返回，只对租户管理员开放。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import pytest
from prometheus_client.parser import text_string_to_metric_families
from xuanpu_server.audit import AuditLog
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.controlplane.models import AuditQuery
from xuanpu_server.metrics import PlatformMetrics
from xuanpu_server.runtime.events import EngineEventHub

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from xuanpu_server.tenancy import Principal

WRITE = {"X-Requested-By": "xuanpu"}
EVER = {"since": "2000-01-01", "until": "2999-12-31"}
"""覆盖全部日子的日期区间：用例跨过 UTC 零点也不漏。"""


def bearer(key: str) -> dict[str, str]:
    """带 API key 的请求头。"""
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
async def ident(identity_world: Any) -> AsyncIterator[Any]:
    """配了 ``public_url`` 的平台，样例业务包已发布并生效。"""
    async with identity_world() as item:
        await item.world.publish()
        yield item


async def employee(ident: Any, **fields: Any) -> tuple[Any, Principal]:
    """建一名员工并让他浏览器登录，返回 ``(员工, 他的调用者身份)``。"""
    user = await ident.user(**fields)
    who: Principal = await ident.world.platform.identity.authenticate_login(await ident.login(user))
    return user, who


def totals(records: list[Any]) -> tuple[int, int, int, int]:
    """几条用量记录的合计：``(输入, 输出, 合计, 轮数)``。"""
    return (
        sum(record.input_tokens for record in records),
        sum(record.output_tokens for record in records),
        sum(record.total_tokens for record in records),
        sum(record.turns for record in records),
    )


async def wait_until(condition: Callable[[], bool], *, seconds: float = 10.0) -> None:
    """轮询直到条件成立；超时就让用例失败。"""
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"等了 {seconds} 秒条件仍不成立")
        await asyncio.sleep(0.01)


async def stop_mid_sample(ident: Any, step: Any, who: Principal) -> str:
    """以 ``who`` 开一个会话，让第一轮卡在采样半路再停掉；返回会话标识。"""
    sessions = ident.world.platform.sessions
    session = await sessions.create(who, package="support-desk")
    ident.world.models.script(step(text="写到一半", wait=asyncio.Event()))
    turn = await sessions.send_message(who, session.session_id, "写")
    events: list[Any] = []

    async def read() -> None:
        """读到这一轮结束。"""
        async for event in turn.stream.events():
            events.append(event)

    task = asyncio.ensure_future(read())
    await wait_until(lambda: any(event.msg.kind == "assistant_text" for event in events))
    assert await sessions.cancel(who, session.session_id) == [turn.submission_id]
    await asyncio.wait_for(task, 10)
    return session.session_id


async def test_user_usage_adds_up_to_the_tenant_total(
    ident: Any, step: Any, read_turn: Any
) -> None:
    """按员工的用量与租户总量一致：员工各自的轮数与 token（含被停下的一轮的估算）分开记；没有
    主人的会话（API key 直接建的）只记进租户总量。"""
    alice, alice_who = await employee(ident, name="爱丽丝")
    bob, bob_who = await employee(ident, name="鲍勃")
    sessions = ident.world.platform.sessions
    # 每次采样记 100 输入、20 输出、120 合计（上一级夹具的脚本默认值）
    ident.world.models.script(*(step(text=f"第{index}句") for index in range(4)))
    for who, count in ((alice_who, 2), (bob_who, 1), (ident.world.member, 1)):
        session = await sessions.create(who, package="support-desk")
        for _ in range(count):
            await read_turn(await sessions.send_message(who, session.session_id, "你好"))
    stopped = await stop_mid_sample(ident, step, alice_who)
    [estimate] = await ident.world.platform.store.query_audit(
        "acme", AuditQuery(session_id=stopped, kind="usage_estimated")
    )
    estimated = estimate.data["estimated_input_tokens"]
    assert estimated > 0

    per_user = await ident.store.list_user_usage("acme", **EVER)
    assert {record.user_id for record in per_user} == {alice.user_id, bob.user_id}
    alice_total = totals([record for record in per_user if record.user_id == alice.user_id])
    bob_total = totals([record for record in per_user if record.user_id == bob.user_id])
    assert alice_total == (200 + estimated, 40, 240 + estimated, 3)
    assert bob_total == (100, 20, 120, 1)
    tenant = totals(await ident.world.platform.store.list_usage("acme", **EVER))
    key_session = (100, 20, 120, 1)
    assert tenant == tuple(
        a + b + k for a, b, k in zip(alice_total, bob_total, key_session, strict=True)
    )
    assert await ident.store.list_user_usage("acme", **EVER, user_id=bob.user_id) == [
        record for record in per_user if record.user_id == bob.user_id
    ]


async def chat(ident: Any, headers: dict[str, str]) -> None:
    """以给定身份（浏览器登录）开一个会话、说一句话（要先给模型排好一步）。"""
    created = await ident.http.post(
        "/v1/sessions", headers=headers, json={"package": "support-desk"}
    )
    session_id = created.json()["session_id"]
    sent = await ident.http.post(
        f"/v1/sessions/{session_id}/messages", headers=headers, json={"text": "你好"}
    )
    assert sent.status_code == 200


async def test_usage_by_user_over_http(ident: Any, step: Any) -> None:
    """``GET /v1/usage/users``：按天、按员工，``user_id`` 只看某人的；租户管理员（API key、浏览器
    登录）可查，成员与代成员调用的不行；两个日期都必填。"""
    alice = await ident.user(name="爱丽丝")
    admin = await ident.user(role="tenant_admin")
    alice_cookie = {**ident.cookie(await ident.login(alice)), **WRITE}
    admin_cookie = {**ident.cookie(await ident.login(admin)), **WRITE}
    ident.world.models.script(step(text="你好"), step(text="你好"))
    await chat(ident, alice_cookie)
    await chat(ident, admin_cookie)

    for headers in (bearer(ident.world.admin_key), admin_cookie):
        everyone = await ident.http.get("/v1/usage/users", headers=headers, params=EVER)
        assert everyone.status_code == 200, everyone.text
        owners = {row["user_id"] for row in everyone.json()["usage"]}
        assert owners == {alice.user_id, admin.user_id}
        params = {**EVER, "user_id": alice.user_id}
        response = await ident.http.get("/v1/usage/users", headers=headers, params=params)
        [row] = response.json()["usage"]
        assert set(row) == {
            "day", "user_id", "input_tokens", "output_tokens", "total_tokens", "turns",
        }  # fmt: skip
        assert (row["user_id"], row["input_tokens"], row["output_tokens"]) == (
            alice.user_id, 100, 20,
        )  # fmt: skip
        assert (row["total_tokens"], row["turns"]) == (120, 1)

    nobody = {**EVER, "user_id": "usr_nobody"}
    response = await ident.http.get("/v1/usage/users", headers=admin_cookie, params=nobody)
    assert response.json() == {"usage": []}

    system = await ident.system_key(role="tenant_admin")
    as_alice = {**bearer(system), "X-Xuanpu-Act-As": alice.user_id}
    for headers in (alice_cookie, bearer(ident.world.member_key), as_alice):
        denied = await ident.http.get("/v1/usage/users", headers=headers, params=EVER)
        assert (denied.status_code, denied.json()["error"]["code"]) == (403, "permission_denied")

    admin_key = bearer(ident.world.admin_key)
    for params in ({"since": "2026-01-01"}, {"since": "2026-01-01", "until": "明天"}):
        bad = await ident.http.get("/v1/usage/users", headers=admin_key, params=params)
        assert (bad.status_code, bad.json()["error"]["code"]) == (400, "invalid_request")


def usage_write_failures(ident: Any) -> float:
    """指标 ``xuanpu_usage_write_failures_total`` 的计数。"""
    text = ident.world.platform.metrics.render().decode()
    return next(
        sample.value
        for family in text_string_to_metric_families(text)
        if family.name == "xuanpu_usage_write_failures"
        for sample in family.samples
        if sample.name == "xuanpu_usage_write_failures_total"
    )


async def test_user_usage_write_failure_is_logged_and_counted(
    ident: Any, step: Any, read_turn: Any, monkeypatch: Any, caplog: Any
) -> None:
    """按员工的那一笔写不进去：这一轮照常结束，租户总量照记；记错误日志并计入
    ``xuanpu_usage_write_failures_total``。"""

    async def broken(*args: Any, **kwargs: Any) -> Any:
        """模拟身份存储写不进去。"""
        raise RuntimeError("身份存储不可用")

    monkeypatch.setattr(ident.store, "add_user_usage", broken)
    _, who = await employee(ident, name="爱丽丝")
    sessions = ident.world.platform.sessions
    session = await sessions.create(who, package="support-desk")
    ident.world.models.script(step(text="你好"))
    with caplog.at_level(logging.ERROR, logger="xuanpu_server.runtime.events"):
        events = await read_turn(await sessions.send_message(who, session.session_id, "你好"))
    assert events[-1].msg.kind == "turn_completed"
    assert totals(await ident.world.platform.store.list_usage("acme", **EVER)) == (100, 20, 120, 1)
    assert await ident.store.list_user_usage("acme", **EVER) == []
    assert usage_write_failures(ident) == 1
    assert "按员工的用量入账失败" in caplog.text


async def test_tenant_usage_failure_skips_the_user_usage(
    ident: Any, step: Any, read_turn: Any, monkeypatch: Any, caplog: Any
) -> None:
    """租户那笔写不进去：员工那笔也不记，各员工之和不大于租户总量；失败照常记日志、计一次。"""

    async def broken(*args: Any, **kwargs: Any) -> Any:
        """模拟控制面写不进去。"""
        raise RuntimeError("控制面不可用")

    monkeypatch.setattr(ident.world.platform.store, "add_usage", broken)
    _, who = await employee(ident, name="爱丽丝")
    sessions = ident.world.platform.sessions
    session = await sessions.create(who, package="support-desk")
    ident.world.models.script(step(text="你好"))
    with caplog.at_level(logging.ERROR, logger="xuanpu_server.runtime.events"):
        events = await read_turn(await sessions.send_message(who, session.session_id, "你好"))
    assert events[-1].msg.kind == "turn_completed"
    assert await ident.store.list_user_usage("acme", **EVER) == []
    assert usage_write_failures(ident) == 1
    assert "的用量入账失败" in caplog.text
    assert "按员工的用量入账失败" not in caplog.text


def test_hub_with_an_owner_needs_the_identity_store() -> None:
    """有主人的会话，事件枢纽必须拿到身份存储，否则按员工的用量无处可记：构造时就报错。"""
    store = InMemoryControlPlane()
    metrics = PlatformMetrics(
        sessions_live=lambda: 0, turns_in_flight=lambda: 0, runtimes_live=lambda: 0
    )
    with pytest.raises(ValueError, match="身份存储"):
        EngineEventHub(
            object(),  # type: ignore[arg-type]
            tenant_id="acme",
            session_id="ses_1",
            audit=AuditLog(store),
            store=store,
            metrics=metrics,
            user_id="usr_1",
        )
