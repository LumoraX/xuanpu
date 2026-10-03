"""指标端点：抓取令牌、Prometheus 文本、各项计数与 gauge、每个平台各自一份。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, get_args

import httpx
import pytest
from prometheus_client import REGISTRY
from prometheus_client.parser import text_string_to_metric_families
from xuanpu_server.api import create_app
from xuanpu_server.audit import AuditLog, digest_arguments
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.identity import LoginFailureReason
from xuanpu_server.metrics import LOGIN_RESULTS, PlatformMetrics
from xuanpu_server.platform import ROOT_PRINCIPAL
from xuanpu_server.runtime.gate_audit import ConnectorToolInfo, gate_call_data
from xuanpu_server.tenancy import generate_metrics_token

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

Samples = dict[tuple[str, tuple[tuple[str, str], ...]], float]

FAMILIES = {
    "xuanpu_sessions_live": "gauge",
    "xuanpu_turns_in_flight": "gauge",
    "xuanpu_runtimes_live": "gauge",
    "xuanpu_turns": "counter",
    "xuanpu_connector_calls": "counter",
    "xuanpu_audit_write_failures": "counter",
    "xuanpu_gate_backfill_failures": "counter",
    "xuanpu_usage_estimated": "counter",
    "xuanpu_usage_write_failures": "counter",
    "xuanpu_logins": "counter",
    "xuanpu_users_disabled": "counter",
    "xuanpu_feishu_events": "counter",
    "xuanpu_reconcile_runs": "counter",
    "xuanpu_reconcile_users": "counter",
}


def bearer(key: str) -> dict[str, str]:
    """带凭据的请求头。"""
    return {"Authorization": f"Bearer {key}"}


def samples(text: str) -> Samples:
    """把指标文本拆成「(样本名, 排好序的标签) → 值」。"""
    return {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(text)
        for sample in family.samples
    }


def value(text: str, name: str, **labels: str) -> float:
    """一个样本的值；没有这个样本就让用例失败。"""
    return samples(text)[(name, tuple(sorted(labels.items())))]


def rendered(metrics: PlatformMetrics) -> str:
    """直接从指标对象出文本（不经 HTTP）。"""
    return metrics.render().decode("utf-8")


async def wait_until(condition: Callable[[], bool], *, timeout: float = 10.0) -> None:
    """轮询直到条件成立；超时就让用例失败。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"等了 {timeout} 秒条件仍不成立")
        await asyncio.sleep(0.01)


@pytest.fixture
async def metered(world_opener: Any, local_sandboxes: Any) -> AsyncIterator[tuple[Any, str]]:
    """开了指标端点的平台，返回 ``(world, 抓取令牌)``。"""
    token, digest = generate_metrics_token()
    world = await world_opener(
        store=InMemoryControlPlane(), sandboxes=local_sandboxes(), metrics_token_hash=digest
    )
    try:
        yield world, token
    finally:
        await world.platform.close()


@pytest.fixture
async def scrape(metered: tuple[Any, str]) -> AsyncIterator[Callable[[], Awaitable[str]]]:
    """经 HTTP 抓一次指标、返回文本的函数。"""
    world, token = metered
    transport = httpx.ASGITransport(app=create_app(world.platform))
    async with httpx.AsyncClient(transport=transport, base_url="http://xuanpu") as http:

        async def get() -> str:
            """抓一次。"""
            response = await http.get("/metrics", headers=bearer(token))
            assert response.status_code == 200, response.text
            return response.text

        yield get


# --- 端点与凭据 ---


async def test_metrics_need_their_own_token(metered: tuple[Any, str]) -> None:
    """配了抓取令牌才开：没有令牌、令牌不对、拿根凭据或 API key 都是同样的 401；
    令牌对了返回 Prometheus 文本。抓取令牌也不能当 API 凭据用。
    """
    world, token = metered
    transport = httpx.ASGITransport(app=create_app(world.platform))
    async with httpx.AsyncClient(transport=transport, base_url="http://xuanpu") as http:
        for headers in (
            {},
            {"Authorization": "Bearer xpmetrics_wrong"},
            {"Authorization": f"Basic {token}"},
            {"Authorization": token},
            bearer(token + "x"),
            bearer(world.root_key),
            bearer(world.admin_key),
        ):
            response = await http.get("/metrics", headers=headers)
            assert response.status_code == 401, headers
            assert response.json() == {"error": {"code": "unauthenticated", "message": "凭据无效"}}

        response = await http.get("/metrics", headers=bearer(token))
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
        families = text_string_to_metric_families(response.text)
        assert {family.name: family.type for family in families} == FAMILIES
        # 计数序列不带 _created（prometheus_client 的 Counter 会多出这一条）
        assert not [name for name, _ in samples(response.text) if name.endswith("_created")]
        assert (await http.get("/v1/sessions", headers=bearer(token))).status_code == 401


async def test_metrics_disabled_without_a_token(world: Any) -> None:
    """没配 metrics.token_hash：/metrics 是 404，带什么凭据都一样。"""
    transport = httpx.ASGITransport(app=create_app(world.platform))
    async with httpx.AsyncClient(transport=transport, base_url="http://xuanpu") as http:
        for headers in ({}, bearer(world.root_key), bearer("xpmetrics_anything")):
            response = await http.get("/metrics", headers=headers)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "not_found"
    assert not world.platform.metrics_enabled


async def test_known_label_values_start_at_zero(scrape: Callable[[], Awaitable[str]]) -> None:
    """已知的标签取值一开始就有序列（值为 0），没发生过的组合也查得到。"""
    text = await scrape()
    for outcome in ("completed", "failed", "suspended", "cancelled"):
        assert value(text, "xuanpu_turns_total", outcome=outcome) == 0
    for outcome in ("resume_rejected", "resume_partial"):
        assert value(text, "xuanpu_turns_total", outcome=outcome) == 0
    for transport in ("stdio", "remote", "http"):
        for decision in ("allowed", "denied", "rejected"):
            labels = {"transport": transport, "decision": decision}
            assert value(text, "xuanpu_connector_calls_total", **labels) == 0
    for stage in ("incomplete", "write", "unconfirmed"):
        assert value(text, "xuanpu_gate_backfill_failures_total", stage=stage) == 0
    assert value(text, "xuanpu_audit_write_failures_total") == 0
    assert value(text, "xuanpu_usage_estimated_total") == 0
    assert value(text, "xuanpu_usage_write_failures_total") == 0


# --- 计数 ---


async def test_turns_and_connector_calls_are_counted(
    metered: tuple[Any, str], scrape: Callable[[], Awaitable[str]], step: Any, read_turn: Any
) -> None:
    """完成、挂起、送回被拒的各记一次 outcome；放行与人工拒绝的连接器调用各记一次 decision。"""
    world, _ = metered
    sessions = world.platform.sessions
    await world.publish()
    session = await sessions.create(world.member, package="support-desk")
    sid = session.session_id

    world.models.script(step(text="您好"))
    await read_turn(await sessions.send_message(world.member, sid, "你好"))
    text = await scrape()
    assert value(text, "xuanpu_turns_total", outcome="completed") == 1

    # 只读的连接器调用直接放行
    world.models.script(
        step(calls=[("c1", "mcp__crm__search_customer", {"name": "张三"})]), step(text="查到了")
    )
    await read_turn(await sessions.send_message(world.member, sid, "查客户"))
    text = await scrape()
    assert value(text, "xuanpu_connector_calls_total", transport="stdio", decision="allowed") == 1
    assert value(text, "xuanpu_turns_total", outcome="completed") == 2

    # 写操作要审批：这一轮挂起；人工拒绝补一条 rejected，续跑的那一轮照常完成
    world.models.script(
        step(calls=[("c2", "mcp__crm__update_ticket", {"id": "T-1"})]), step(text="不改了")
    )
    suspended = await read_turn(await sessions.send_message(world.member, sid, "改工单"))
    assert suspended[-1].msg.kind == "turn_suspended"
    request_id = (await sessions.pending(world.member, sid))[0]["request_id"]
    resolutions = {request_id: {"granted": False}}
    await read_turn(await sessions.resume(world.member, sid, resolutions))
    text = await scrape()
    assert value(text, "xuanpu_turns_total", outcome="suspended") == 1
    assert value(text, "xuanpu_turns_total", outcome="completed") == 3
    assert value(text, "xuanpu_connector_calls_total", transport="stdio", decision="rejected") == 1
    assert value(text, "xuanpu_connector_calls_total", transport="stdio", decision="allowed") == 1

    # 送回一个不存在的待办：内核整次拒绝，记 resume_rejected，不再多一条 rejected
    nope = {"req_nope": {"granted": False}}
    again = await read_turn(await sessions.resume(world.member, sid, nope), timeout=10)
    assert again[-1].msg.kind == "suspension_resolve_rejected"
    text = await scrape()
    assert value(text, "xuanpu_turns_total", outcome="resume_rejected") == 1
    assert value(text, "xuanpu_connector_calls_total", transport="stdio", decision="rejected") == 1
    assert value(text, "xuanpu_turns_total", outcome="failed") == 0


async def test_partial_resume_is_its_own_outcome(
    metered: tuple[Any, str], step: Any, read_turn: Any
) -> None:
    """一次挂起两个审批、只送回一个：那次送回以 suspension_partially_resolved 结束，
    记 resume_partial。"""
    world, _ = metered
    sessions = world.platform.sessions
    await world.publish()
    session = await sessions.create(world.member, package="support-desk")
    calls = [
        ("c1", "mcp__crm__update_ticket", {"id": "T-1"}),
        ("c2", "mcp__crm__update_ticket", {"id": "T-2"}),
    ]
    world.models.script(step(calls=calls), step(text="好了"))
    await read_turn(await sessions.send_message(world.member, session.session_id, "改两个"))
    first = (await sessions.pending(world.member, session.session_id))[0]["request_id"]
    partial = await read_turn(
        await sessions.resume(world.member, session.session_id, {first: {"granted": True}}),
        timeout=10,
    )
    assert partial[-1].msg.kind == "suspension_partially_resolved"
    text = rendered(world.platform.metrics)
    assert value(text, "xuanpu_turns_total", outcome="resume_partial") == 1
    assert value(text, "xuanpu_turns_total", outcome="suspended") == 1


async def test_cancelled_turn_and_the_in_flight_gauge(
    metered: tuple[Any, str], scrape: Callable[[], Awaitable[str]], step: Any
) -> None:
    """停止生成：进行中的那一轮算在 turns_in_flight 里；停下之后记 cancelled、按估算入账一次。"""
    world, _ = metered
    sessions = world.platform.sessions
    await world.publish()
    session = await sessions.create(world.member, package="support-desk")
    gate = asyncio.Event()
    world.models.script(step(text="写到一半", wait=gate))
    turn = await sessions.send_message(world.member, session.session_id, "写长文")
    events: list[Any] = []

    async def read() -> None:
        """读到这一轮结束。"""
        async for event in turn.stream.events():
            events.append(event)

    task = asyncio.ensure_future(read())
    await wait_until(lambda: any(event.msg.kind == "assistant_text" for event in events))
    assert value(await scrape(), "xuanpu_turns_in_flight") == 1

    assert await sessions.cancel(world.member, session.session_id) == [turn.submission_id]
    await asyncio.wait_for(task, 10)
    assert events[-1].msg.data["end_reason"] == "cancelled"
    text = await scrape()
    assert value(text, "xuanpu_turns_in_flight") == 0
    assert value(text, "xuanpu_turns_total", outcome="cancelled") == 1
    assert value(text, "xuanpu_turns_total", outcome="completed") == 0
    assert value(text, "xuanpu_usage_estimated_total") == 1


async def test_usage_write_failures_are_counted(
    metered: tuple[Any, str],
    step: Any,
    read_turn: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """一轮结束时用量写不进去：这一轮照常收尾，记 error 日志，计一次用量入账失败。"""
    world, _ = metered
    sessions = world.platform.sessions
    await world.publish()
    session = await sessions.create(world.member, package="support-desk")

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("控制面写不进去")

    monkeypatch.setattr(world.platform.store, "add_usage", broken)
    world.models.script(step(text="您好"))
    with caplog.at_level("ERROR", logger="xuanpu_server.runtime.events"):
        events = await read_turn(await sessions.send_message(world.member, session.session_id, "1"))
    assert events[-1].msg.kind == "turn_completed"
    assert "用量入账失败" in caplog.text
    text = rendered(world.platform.metrics)
    assert value(text, "xuanpu_usage_write_failures_total") == 1
    assert value(text, "xuanpu_turns_total", outcome="completed") == 1


async def test_gauges_follow_sessions_and_runtimes(
    metered: tuple[Any, str], step: Any, read_turn: Any
) -> None:
    """持有的会话数、运行时数反映当前值：建会话不开引擎，对话之后才有；关闭、回收之后回落。"""
    world, _ = metered
    platform = world.platform

    def gauges() -> tuple[float, float, float]:
        text = rendered(platform.metrics)
        return (
            value(text, "xuanpu_sessions_live"),
            value(text, "xuanpu_runtimes_live"),
            value(text, "xuanpu_turns_in_flight"),
        )

    await world.publish()
    first = await platform.sessions.create(world.member, package="support-desk")
    second = await platform.sessions.create(world.member, package="support-desk")
    assert gauges() == (0, 0, 0)
    world.models.script(step(text="一"), step(text="二"))
    await read_turn(await platform.sessions.send_message(world.member, first.session_id, "1"))
    assert gauges() == (1, 1, 0)
    await read_turn(await platform.sessions.send_message(world.member, second.session_id, "2"))
    assert gauges() == (2, 1, 0)

    await platform.sessions.close(world.member, first.session_id)
    await platform.sessions.close(world.member, second.session_id)
    assert gauges() == (0, 1, 0)
    # 运行时空闲超过 runtime_idle_seconds 才回收：把它最近一次被使用的时刻往回拨
    key = ("acme", "support-desk", "1.0.0")
    runtime = platform.runtimes._runtimes[key]  # noqa: SLF001
    runtime.last_used -= platform.runtimes.defaults.runtime_idle_seconds
    assert await platform.runtimes.evict_idle() == [key]
    assert gauges() == (0, 0, 0)


async def test_metrics_carry_no_tenant_session_or_secret(
    metered: tuple[Any, str], scrape: Callable[[], Awaitable[str]], step: Any, read_turn: Any
) -> None:
    """返回体里没有租户、会话、业务包、连接器的标识，也没有任何凭据与密钥；标签只有固定的几种。"""
    world, token = metered
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(
        step(calls=[("c1", "mcp__crm__search_customer", {"name": "张三"})]), step(text="好")
    )
    turn = await world.platform.sessions.send_message(world.member, session.session_id, "查")
    await read_turn(turn)
    text = await scrape()
    assert value(text, "xuanpu_connector_calls_total", transport="stdio", decision="allowed") == 1
    for secret in (
        "acme",
        session.session_id,
        turn.submission_id,
        "support-desk",
        "crm",
        "inst-test",
        "s3cret-token-value",
        world.root_key,
        world.admin_key,
        world.member_key,
        token,
    ):
        assert secret not in text, secret
    labels = {name for _, pairs in samples(text) for name, _ in pairs}
    assert labels == {"outcome", "transport", "decision", "stage", "result", "reason"}


def test_login_results_match_the_failure_reasons() -> None:
    """``xuanpu_logins_total`` 的取值：成功，加上与身份域一致的七种失败原因（指标模块另写一份，
    运行时不依赖身份域）。"""
    assert LOGIN_RESULTS == ("succeeded", *get_args(LoginFailureReason))


# --- 每个平台各自一份 ---


async def test_each_platform_has_its_own_registry(
    metered: tuple[Any, str], tmp_path: Path, step: Any, read_turn: Any
) -> None:
    """同一进程里的两个平台各自计数；进程级的全局登记表里没有平台的指标。"""
    world, _ = metered
    other = world.sibling("inst-other", data_dir=(tmp_path / "data-other").resolve())
    try:
        await world.publish()
        session = await world.platform.sessions.create(world.member, package="support-desk")
        world.models.script(step(text="好"))
        await read_turn(
            await world.platform.sessions.send_message(world.member, session.session_id, "1")
        )
        ours, theirs = rendered(world.platform.metrics), rendered(other.metrics)
        assert value(ours, "xuanpu_turns_total", outcome="completed") == 1
        assert value(theirs, "xuanpu_turns_total", outcome="completed") == 0
        assert other.metrics.registry is not world.platform.metrics.registry
        global_names = {family.name for family in REGISTRY.collect()}
        assert not [name for name in global_names if name.startswith("xuanpu_")]
    finally:
        await other.close()


def test_standalone_metrics_count_independently() -> None:
    """两份指标互不影响；gauge 在抓取时调回调现取。"""
    live = [3]
    first = PlatformMetrics(
        sessions_live=lambda: live[0], turns_in_flight=lambda: 0, runtimes_live=lambda: 0
    )
    second = PlatformMetrics(
        sessions_live=lambda: 0, turns_in_flight=lambda: 0, runtimes_live=lambda: 0
    )
    first.turn_ended("failed")
    first.connector_call("http", "denied")
    first.backfill_failed("write")
    assert value(rendered(first), "xuanpu_sessions_live") == 3
    live[0] = 5
    assert value(rendered(first), "xuanpu_sessions_live") == 5
    assert value(rendered(first), "xuanpu_turns_total", outcome="failed") == 1
    assert value(rendered(second), "xuanpu_turns_total", outcome="failed") == 0
    labels = {"transport": "http", "decision": "denied"}
    assert value(rendered(first), "xuanpu_connector_calls_total", **labels) == 1
    assert value(rendered(second), "xuanpu_connector_calls_total", **labels) == 0
    assert value(rendered(first), "xuanpu_gate_backfill_failures_total", stage="write") == 1


async def test_audit_writes_count_failures_and_connector_calls() -> None:
    """审计写入口：三种来源写失败各计一次（原样上抛）；过闸记录不论写没写成都计一次调用。"""
    store = InMemoryControlPlane()
    metrics = PlatformMetrics(
        sessions_live=lambda: 0, turns_in_flight=lambda: 0, runtimes_live=lambda: 0
    )
    audit = AuditLog(store, metrics=metrics)
    info = ConnectorToolInfo(connector="docs", transport="remote", tool="read_doc", effect="read")
    data = gate_call_data(
        info,
        decision="allowed",
        is_error=False,
        detail="",
        call_id="call-1",
        thread_id="thr_1",
        arguments_digest=digest_arguments({}),
        submission_id="sub_1",
        duration_ms=3,
    )
    await audit.gate("acme", "connector_call", data, session_id="ses_1")
    await audit.control(ROOT_PRINCIPAL, "acme", "tenant_created", {"name": "Acme"})
    text = rendered(metrics)
    assert value(text, "xuanpu_connector_calls_total", transport="remote", decision="allowed") == 1
    assert value(text, "xuanpu_audit_write_failures_total") == 0

    async def broken(records: Any) -> int:
        raise RuntimeError("控制面写不进去")

    store.append_audit = broken  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await audit.control(ROOT_PRINCIPAL, "acme", "tenant_created", {"name": "Acme"})
    with pytest.raises(RuntimeError):
        await audit.gate("acme", "connector_call", data, session_id="ses_1")
    with pytest.raises(RuntimeError):
        await audit.engine([])
    text = rendered(metrics)
    assert value(text, "xuanpu_audit_write_failures_total") == 3
    assert value(text, "xuanpu_connector_calls_total", transport="remote", decision="allowed") == 2
