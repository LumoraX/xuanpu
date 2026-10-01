"""远端 MCP 连接器（``transport: remote``）：经 Streamable HTTP 接一个真的、跑在本机端口上的服务。

假的远端 MCP server 只做最小的一套：``POST /mcp`` 收 JSON-RPC、回 ``application/json``；
``GET /mcp`` 回 405（不提供推送流）；``DELETE /mcp`` 结束会话。它记下每次请求的方法、
``Authorization`` 头、会话头与请求体，用例据此核对平台实际发出去的东西。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import taifeng
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from xuanpu_server.controlplane import AuditQuery
from xuanpu_server.errors import RuntimeUnavailableError, SecretMissingError
from xuanpu_server.runtime.connector_process import KernelMcpLogGuard
from xuanpu_server.runtime.remote_mcp import VaultAuthTransport
from xuanpu_spec import pack_package
from xuanpu_spec.manifest import HttpAuth

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from xuanpu_server.runtime import TenantRuntime

TOKEN = "tok-123"
SESSION = "sess-1"

# 远端提供的工具；admin_purge 不在清单的白名单里
TOOLS: list[dict[str, Any]] = [
    {
        "name": "search",
        "description": "搜文档",
        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
    },
    {"name": "update_doc", "description": "改文档", "inputSchema": {"type": "object"}},
    {"name": "fail", "description": "总是报错", "inputSchema": {"type": "object"}},
    {"name": "slow", "description": "很慢", "inputSchema": {"type": "object"}},
    {"name": "admin_purge", "description": "清空全部文档", "inputSchema": {"type": "object"}},
]


@dataclass
class RemoteState:
    """假 server 记下的东西。

    Attributes:
        requests: 每次请求的 ``{http, rpc, authorization, session, body}``。
        release: 置位后 ``slow`` 工具才返回。
        fail_initialize: 握手时回 401，响应体里回显收到的 ``Authorization`` 头。
    """

    requests: list[dict[str, Any]] = field(default_factory=list)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    fail_initialize: bool = False

    @property
    def auth_headers(self) -> list[str | None]:
        """每次请求带的 ``Authorization`` 头。"""
        return [item["authorization"] for item in self.requests]

    def rpc(self, method: str) -> list[dict[str, Any]]:
        """某个 JSON-RPC 方法的全部请求。"""
        return [item for item in self.requests if item["rpc"] == method]


class FakeRemoteMcp:
    """最小的 MCP Streamable HTTP 服务。本身就是 ASGI 应用。"""

    def __init__(self) -> None:
        self.state = RemoteState()
        self.app = FastAPI()
        app = self.app

        @app.post("/mcp")
        async def rpc(request: Request) -> Response:
            message = await request.json()
            self._record(request, message)
            return await self._answer(request, message)

        @app.get("/mcp")
        async def push_stream(request: Request) -> Response:
            self._record(request, None)
            return Response(status_code=405)

        @app.delete("/mcp")
        async def end_session(request: Request) -> Response:
            self._record(request, None)
            return Response(status_code=204)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """ASGI 入口。"""
        await self.app(scope, receive, send)

    def _record(self, request: Request, message: dict[str, Any] | None) -> None:
        """记下一次请求。"""
        self.state.requests.append(
            {
                "http": request.method,
                "rpc": None if message is None else message.get("method"),
                "authorization": request.headers.get("authorization"),
                "session": request.headers.get("mcp-session-id"),
                "body": message,
            }
        )

    async def _answer(self, request: Request, message: dict[str, Any]) -> Response:
        """按 JSON-RPC 方法回应。通知回 202；握手之后的请求必须带会话头。"""
        if "id" not in message:
            return Response(status_code=202)
        method = message["method"]
        if method == "initialize":
            if self.state.fail_initialize:
                echoed = f"拒绝了 {request.headers.get('authorization')}"
                return JSONResponse({"error": echoed}, status_code=401)
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-docs", "version": "1"},
            }
            return JSONResponse(
                {"jsonrpc": "2.0", "id": message["id"], "result": result},
                headers={"Mcp-Session-Id": SESSION},
            )
        if request.headers.get("mcp-session-id") != SESSION:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        if method == "tools/list":
            return _result(message, {"tools": TOOLS})
        if method == "tools/call":
            return _result(message, await self._call(request, message["params"]))
        error = {"code": -32601, "message": f"method not found: {method}"}
        return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "error": error})

    async def _call(self, request: Request, params: dict[str, Any]) -> dict[str, Any]:
        """执行一个工具。``fail`` 回 isError 并回显凭据；``slow`` 等到放行。"""
        name, arguments = params["name"], params.get("arguments", {})
        if name == "slow":
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.state.release.wait(), timeout=10)
        if name == "fail":
            text = f"后端拒绝了 {request.headers.get('authorization')}"
            return {"isError": True, "content": [{"type": "text", "text": text}]}
        text = f"{name}: {json.dumps(arguments, ensure_ascii=False)}"
        return {"content": [{"type": "text", "text": text}]}


def _result(message: dict[str, Any], result: dict[str, Any]) -> JSONResponse:
    """JSON-RPC 成功响应。"""
    return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": result})


@pytest.fixture
def fake_remote_mcp() -> FakeRemoteMcp:
    """一个新的假远端 MCP server（还没跑起来，交给 ``serve``）。"""
    return FakeRemoteMcp()


@pytest.fixture
async def serve() -> AsyncIterator[Any]:
    """返回一个函数：把一个 ASGI 应用真的跑在本机端口上，返回它的地址。用例结束时全部停掉。

    与 ``test_http_connector.py`` 里的同名夹具一样（测试模块之间不能互相 import）。
    """
    import socket

    import uvicorn

    running: list[tuple[uvicorn.Server, asyncio.Task[None]]] = []

    async def start(app: Any) -> str:
        """在一个空闲端口上起服务，等它就绪后返回地址。"""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        task = asyncio.ensure_future(server.serve())
        running.append((server, task))
        for _ in range(200):
            if server.started:
                return f"http://127.0.0.1:{port}"
            await asyncio.sleep(0.02)
        raise RuntimeError("HTTP 服务没有启动")

    try:
        yield start
    finally:
        for server, task in running:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10)


MANIFEST = """\
apiVersion: xuanpu/v1
metadata: {{name: docs-desk, version: 1.0.0}}
spec:
  skills: {{entries: [helper]}}
  policy: {{approval: standard}}
  connectors:
    - name: docs
      transport: remote
      url: {url}
      auth: {auth}
      timeoutSeconds: {timeout}
      tools:
        - {{name: search, effect: read}}
        - {{name: update_doc, effect: write}}
        - {{name: fail, effect: read}}
        - {{name: slow, effect: read}}{extra_tools}
"""

HELPER = """\
---
name: helper
description: 文档助手
version: 1.0.0
type: composite
entry: true
child_skills: [faq]
tool_names: [mcp__docs__search, mcp__docs__update_doc, mcp__docs__fail, mcp__docs__slow]
---
# 文档助手
"""

FAQ = "---\nname: faq\ndescription: 常见问题\nversion: 1.0.0\ntype: atomic\n---\n正文\n"


async def publish_remote_package(
    world: Any,
    *,
    url: str,
    auth: str = "{type: bearer, secret: docs-token}",
    timeout: float = 5,
    extra_tools: str = "",
) -> None:
    """发布一个带 remote 连接器 ``docs`` 的业务包 ``docs-desk`` 并设为生效版本。"""
    root: Path = world.package_dir / "docs-desk"
    (root / "skills" / "helper").mkdir(parents=True)
    (root / "skills" / "faq").mkdir(parents=True)
    (root / "xuanpu.yaml").write_text(
        MANIFEST.format(url=url, auth=auth, timeout=timeout, extra_tools=extra_tools),
        encoding="utf-8",
    )
    (root / "skills" / "helper" / "SKILL.md").write_text(HELPER, encoding="utf-8")
    (root / "skills" / "faq" / "SKILL.md").write_text(FAQ, encoding="utf-8")
    await world.platform.packages.publish(world.admin, pack_package(root), activate=True)


async def ready(world: Any, serve: Any, fake: FakeRemoteMcp, **kwargs: Any) -> str:
    """跑起假 server、发布业务包、配好密钥 ``docs-token``，返回建好的会话标识。"""
    base = await serve(fake)
    await publish_remote_package(world, url=f"{base}/mcp", **kwargs)
    await world.platform.put_secret(world.admin, "docs-token", TOKEN)
    session = await world.platform.sessions.create(world.member, package="docs-desk")
    return str(session.session_id)


async def ask(
    world: Any, step: Any, read_turn: Any, session_id: str, calls: list[Any]
) -> list[Any]:
    """在会话里让模型发起给定的工具调用（之后回一句话），返回这一轮的事件。"""
    world.models.script(step(calls=calls), step(text="好"))
    turn = await world.platform.sessions.send_message(world.member, session_id, "办事")
    return list(await read_turn(turn))


def output_of(events: list[Any], call_id: str = "c1") -> dict[str, Any]:
    """某次工具调用的结果事件数据。"""
    return next(
        e.msg.data
        for e in events
        if e.msg.kind == "tool_call_completed" and e.msg.data["call_id"] == call_id
    )


async def gate_records(world: Any) -> list[Any]:
    """全部过闸记录。"""
    return list(
        await world.platform.store.query_audit("acme", AuditQuery(source="gate", limit=100))
    )


async def everything(world: Any) -> str:
    """全部审计记录的数据，拼成一段文本（查明文用）。"""
    records = await world.platform.store.query_audit("acme", AuditQuery(limit=1000))
    return json.dumps([r.data for r in records], ensure_ascii=False)


def only_runtime(world: Any) -> TenantRuntime:
    """平台里唯一一个已组装的运行时。"""
    runtimes = list(world.platform.runtimes._runtimes.values())  # noqa: SLF001
    assert len(runtimes) == 1
    return runtimes[0]


# ---------------------------------------------------------------------------
# 经平台走一遍
# ---------------------------------------------------------------------------


async def test_remote_mcp_tools_go_through_the_gate(
    world: Any, serve: Any, step: Any, read_turn: Any, fake_remote_mcp: FakeRemoteMcp
) -> None:
    """远端 MCP 的工具与 stdio 一样过闸：读操作放行并留过闸记录；凭据按名字注入。"""
    base = await serve(fake_remote_mcp)
    await publish_remote_package(world, url=f"{base}/mcp")
    await world.platform.put_secret(world.admin, "docs-token", TOKEN)
    session = await world.platform.sessions.create(world.member, package="docs-desk")
    world.models.script(
        step(calls=[("c1", "mcp__docs__search", {"q": "合同"})]), step(text="找到了")
    )
    events = await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "找合同")
    )
    assert events[-1].msg.kind == "turn_completed"
    result = output_of(events)
    assert (result["is_error"], result["output"]) == (False, 'search: {"q": "合同"}')
    state = fake_remote_mcp.state
    assert state.auth_headers and set(state.auth_headers) == {f"Bearer {TOKEN}"}
    assert [call["body"]["params"] for call in state.rpc("tools/call")] == [
        {"name": "search", "arguments": {"q": "合同"}}
    ]
    gate = await gate_records(world)
    assert gate[-1].data["transport"] == "remote" and gate[-1].data["decision"] == "allowed"
    assert (gate[-1].data["tool"], gate[-1].data["effect"]) == ("search", "read")
    assert gate[-1].session_id == session.session_id
    assert TOKEN not in json.dumps([r.data for r in gate], ensure_ascii=False)
    assert TOKEN not in await everything(world)
    assert TOKEN not in world.models.last_prompt()


async def test_missing_secret_blocks_the_runtime(
    world: Any, serve: Any, fake_remote_mcp: FakeRemoteMcp
) -> None:
    """密钥没配：对话起不来、说清楚缺哪条（与 HTTP 连接器一致），远端一个请求都没收到。"""
    base = await serve(fake_remote_mcp)
    await publish_remote_package(world, url=f"{base}/mcp")
    session = await world.platform.sessions.create(world.member, package="docs-desk")
    with pytest.raises(SecretMissingError, match="docs-token"):
        await world.platform.sessions.send_message(world.member, session.session_id, "办事")
    assert fake_remote_mcp.state.requests == []


async def test_rotated_secret_takes_effect_on_the_next_request(
    world: Any, serve: Any, step: Any, read_turn: Any, fake_remote_mcp: FakeRemoteMcp
) -> None:
    """两次调用之间轮换密钥：同一个客户端（只握手一次）的下一次请求就带新值。

    这证明客户端没有长期持有明文：凭据是每次请求发出时从保管库现取的。
    """
    session_id = await ready(world, serve, fake_remote_mcp)
    await ask(world, step, read_turn, session_id, [("c1", "mcp__docs__search", {"q": "一"})])
    await world.platform.put_secret(world.admin, "docs-token", "tok-456")
    await ask(world, step, read_turn, session_id, [("c1", "mcp__docs__search", {"q": "二"})])
    state = fake_remote_mcp.state
    assert [call["authorization"] for call in state.rpc("tools/call")] == [
        f"Bearer {TOKEN}",
        "Bearer tok-456",
    ]
    assert len(state.rpc("initialize")) == 1


async def test_unreachable_remote_fails_assembly_with_type_only(
    world: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """远端连不上：组装失败，提示只带异常类型、不链原异常；日志里有排障线索。"""
    await publish_remote_package(world, url="http://127.0.0.1:1/mcp")
    await world.platform.put_secret(world.admin, "docs-token", TOKEN)
    session = await world.platform.sessions.create(world.member, package="docs-desk")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(RuntimeUnavailableError) as caught:
        await world.platform.sessions.send_message(world.member, session.session_id, "办事")
    assert str(caught.value) == "连接器 'docs' 启动失败：McpToolError"
    assert caught.value.__cause__ is None
    [logged] = [r.getMessage() for r in caplog.records if "连接器 docs 启动失败" in r.getMessage()]
    assert "ConnectError" in logged
    assert world.platform.runtimes._runtimes == {}  # noqa: SLF001


async def test_failed_handshake_is_logged_redacted(
    world: Any, serve: Any, fake_remote_mcp: FakeRemoteMcp, caplog: pytest.LogCaptureFixture
) -> None:
    """握手被拒、远端在响应体里回显了凭据：提示只带异常类型；日志里的凭据按值换成 ***。"""
    fake_remote_mcp.state.fail_initialize = True
    session_id = await ready(world, serve, fake_remote_mcp)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(RuntimeUnavailableError) as caught:
        await world.platform.sessions.send_message(world.member, session_id, "办事")
    assert str(caught.value) == "连接器 'docs' 启动失败：McpToolError"
    assert caught.value.__cause__ is None
    [logged] = [r.getMessage() for r in caplog.records if "连接器 docs 启动失败" in r.getMessage()]
    assert "http 401" in logged
    assert "Bearer ***" in logged
    assert TOKEN not in caplog.text


async def test_only_whitelisted_remote_tools_are_exposed(
    world: Any, serve: Any, step: Any, read_turn: Any, fake_remote_mcp: FakeRemoteMcp
) -> None:
    """远端多提供的工具（不在白名单里）不登记、模型看不到；副作用分类按清单。"""
    session_id = await ready(world, serve, fake_remote_mcp)
    await ask(world, step, read_turn, session_id, [("c1", "mcp__docs__admin_purge", {})])
    effects = only_runtime(world).tool_effects
    assert {name: effects[name] for name in effects if name.startswith("mcp__docs__")} == {
        "mcp__docs__search": "read",
        "mcp__docs__update_doc": "write",
        "mcp__docs__fail": "read",
        "mcp__docs__slow": "read",
    }
    offered = {tool["name"] for tool in world.models.requests[0].model_dump()["tools"]}
    assert "mcp__docs__search" in offered
    assert "mcp__docs__admin_purge" not in offered
    assert fake_remote_mcp.state.rpc("tools/call") == []


async def test_declared_tool_missing_on_remote_fails_assembly(
    world: Any, serve: Any, fake_remote_mcp: FakeRemoteMcp
) -> None:
    """清单声明了、远端却没有的工具：组装失败并说出缺哪个；已建的会话随之结束（DELETE）。"""
    session_id = await ready(world, serve, fake_remote_mcp, extra_tools="\n        - {name: ghost}")
    with pytest.raises(RuntimeUnavailableError, match="没有提供清单里声明的工具：ghost"):
        await world.platform.sessions.send_message(world.member, session_id, "办事")
    deletes = [item for item in fake_remote_mcp.state.requests if item["http"] == "DELETE"]
    assert [item["session"] for item in deletes] == [SESSION]


async def test_remote_write_tool_waits_for_approval(
    world: Any, serve: Any, step: Any, read_turn: Any, fake_remote_mcp: FakeRemoteMcp
) -> None:
    """写操作在 standard 审批下挂起；批准后才真的调用，过闸记录归到 resume 那一轮。"""
    session_id = await ready(world, serve, fake_remote_mcp)
    arguments = {"id": "D-1", "text": "新内容"}
    events = await ask(
        world, step, read_turn, session_id, [("c1", "mcp__docs__update_doc", arguments)]
    )
    assert events[-1].msg.kind == "turn_suspended"
    assert fake_remote_mcp.state.rpc("tools/call") == []
    pending = events[-1].msg.data["pending"][0]
    assert pending["detail"]["target"] == "mcp__docs__update_doc"

    resumed = await world.platform.sessions.resume(
        world.member, session_id, {pending["request_id"]: {"granted": True}}
    )
    resumed_events = await read_turn(resumed)
    assert resumed_events[-1].msg.kind == "turn_completed"
    assert [call["body"]["params"] for call in fake_remote_mcp.state.rpc("tools/call")] == [
        {"name": "update_doc", "arguments": arguments}
    ]
    gate = await gate_records(world)
    assert [
        (r.data["transport"], r.data["tool"], r.data["effect"], r.data["decision"]) for r in gate
    ] == [("remote", "update_doc", "write", "allowed")]
    assert gate[0].data["submission_id"] == resumed.submission_id
    assert "新内容" not in json.dumps(gate[0].data, ensure_ascii=False)


async def test_remote_tool_error_is_redacted(
    world: Any,
    serve: Any,
    step: Any,
    read_turn: Any,
    fake_remote_mcp: FakeRemoteMcp,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """远端工具回 isError 且回显了凭据：模型拿到的是错误结果，凭据换成 ***；审计记为出错。"""
    caplog.set_level(logging.DEBUG)
    session_id = await ready(world, serve, fake_remote_mcp)
    events = await ask(world, step, read_turn, session_id, [("c1", "mcp__docs__fail", {})])
    result = output_of(events)
    assert result["is_error"] is True
    assert "后端拒绝了 Bearer ***" in result["output"]
    assert TOKEN not in result["output"]
    assert TOKEN not in world.models.last_prompt()
    gate = await gate_records(world)
    assert [(r.data["tool"], r.data["decision"], r.data["is_error"]) for r in gate] == [
        ("fail", "allowed", True)
    ]
    assert TOKEN not in await everything(world)
    assert TOKEN not in caplog.text


async def test_slow_remote_tool_times_out(
    world: Any, serve: Any, step: Any, read_turn: Any, fake_remote_mcp: FakeRemoteMcp
) -> None:
    """远端慢于 timeoutSeconds：这次调用记为 connector_timeout，并通知远端取消这次请求。"""
    session_id = await ready(world, serve, fake_remote_mcp, timeout=0.5)
    started = time.monotonic()
    try:
        events = await ask(world, step, read_turn, session_id, [("c1", "mcp__docs__slow", {})])
        assert time.monotonic() - started < 5
        result = output_of(events)
        assert (result["is_error"], result["output"]) == (True, "connector_timeout")
        gate = await gate_records(world)
        assert [(r.data["tool"], r.data["is_error"], r.data["detail"]) for r in gate] == [
            ("slow", True, "timeout")
        ]
        # 内核放弃请求时另发 notifications/cancelled（后台发送，等它片刻）
        for _ in range(100):
            if fake_remote_mcp.state.rpc("notifications/cancelled"):
                break
            await asyncio.sleep(0.05)
        [cancelled] = fake_remote_mcp.state.rpc("notifications/cancelled")
        [call] = fake_remote_mcp.state.rpc("tools/call")
        assert cancelled["body"]["params"]["requestId"] == call["body"]["id"]
    finally:
        fake_remote_mcp.state.release.set()


async def test_runtime_eviction_closes_the_remote_session(
    world: Any, serve: Any, step: Any, read_turn: Any, fake_remote_mcp: FakeRemoteMcp
) -> None:
    """运行时回收时客户端被关闭：远端收到结束会话的 DELETE（照常带凭据与会话头）。"""
    session_id = await ready(world, serve, fake_remote_mcp)
    await ask(world, step, read_turn, session_id, [("c1", "mcp__docs__search", {"q": "x"})])
    assert [item for item in fake_remote_mcp.state.requests if item["http"] == "DELETE"] == []
    runtime = only_runtime(world)
    assert await world.platform.runtimes.evict(runtime.key) is True
    deletes = [item for item in fake_remote_mcp.state.requests if item["http"] == "DELETE"]
    assert [(item["session"], item["authorization"]) for item in deletes] == [
        (SESSION, f"Bearer {TOKEN}")
    ]


async def test_client_is_built_without_plaintext_headers(
    world: Any,
    serve: Any,
    step: Any,
    read_turn: Any,
    fake_remote_mcp: FakeRemoteMcp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """组装时不把明文放进客户端的 headers：凭据只经自定义传输在请求发出时加上。

    同时钉住：单次请求超时取清单与平台超时的小者；不开 GET 推送流（远端一个 GET 都没收到）。
    """
    seen: list[dict[str, Any]] = []
    clients: list[taifeng.McpHttpClient] = []
    original = taifeng.McpHttpClient.connect

    async def spy(url: str, **kwargs: Any) -> taifeng.McpHttpClient:
        """记下建连参数与建好的客户端。"""
        seen.append(kwargs)
        client = await original(url, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(taifeng.McpHttpClient, "connect", spy)
    session_id = await ready(world, serve, fake_remote_mcp)
    await ask(world, step, read_turn, session_id, [("c1", "mcp__docs__search", {"q": "x"})])

    [kwargs] = seen
    assert kwargs.get("headers") is None
    assert isinstance(kwargs["transport"], VaultAuthTransport)
    assert kwargs["listen_notifications"] is False
    assert kwargs["request_timeout_seconds"] == 5.0
    assert TOKEN not in repr(kwargs)
    assert TOKEN not in repr(vars(kwargs["transport"]))
    [client] = clients
    assert "authorization" not in client._http.headers  # noqa: SLF001
    assert [item for item in fake_remote_mcp.state.requests if item["http"] == "GET"] == []
    assert set(fake_remote_mcp.state.auth_headers) == {f"Bearer {TOKEN}"}


async def test_kernel_http_client_logs_are_guarded(world: Any) -> None:
    """平台装配后，内核 HTTP MCP 客户端的 logger 与 stdio 客户端一样挂着日志卫生过滤器。"""
    filters = logging.getLogger("taifeng.mcp.http_client").filters
    assert len([item for item in filters if isinstance(item, KernelMcpLogGuard)]) == 1


# ---------------------------------------------------------------------------
# 凭据传输本身
# ---------------------------------------------------------------------------


class FakeVault:
    """记下每次取值的保管库。"""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = dict(values)
        self.reads: list[tuple[str, str]] = []

    async def get(self, tenant_id: str, name: str) -> str:
        """取明文；没有就报缺密钥。"""
        self.reads.append((tenant_id, name))
        if name not in self.values:
            raise SecretMissingError(f"密钥 {name!r} 尚未配置")
        return self.values[name]


def recording_inner() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """一个记下收到的请求、一律回 200 的内层传输。"""
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler), received


def vault_transport(
    inner: httpx.AsyncBaseTransport, vault: FakeVault, auth: HttpAuth | None
) -> VaultAuthTransport:
    """指向 ``https://mcp.example.com/mcp`` 的凭据传输。"""
    return VaultAuthTransport(
        inner, url="https://mcp.example.com/mcp", auth=auth, tenant_id="acme", vault=vault
    )


async def test_vault_transport_fetches_the_secret_for_every_request() -> None:
    """每次请求都从保管库现取：轮换后下一次请求就是新值；传输对象里没有明文。"""
    inner, received = recording_inner()
    vault = FakeVault({"docs-token": "first-value"})
    transport = vault_transport(inner, vault, HttpAuth(type="bearer", secret="docs-token"))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post("https://mcp.example.com/mcp", json={})
        assert "first-value" not in repr(vars(transport))
        vault.values["docs-token"] = "second-value"
        await client.post("https://mcp.example.com/mcp", json={})
    assert [request.headers["authorization"] for request in received] == [
        "Bearer first-value",
        "Bearer second-value",
    ]
    assert vault.reads == [("acme", "docs-token"), ("acme", "docs-token")]


async def test_vault_transport_header_style_and_no_auth() -> None:
    """header 方式放到指定的头；不声明凭据时什么也不加，也不读保管库。"""
    inner, received = recording_inner()
    vault = FakeVault({"docs-key": "k-1"})
    header = HttpAuth(type="header", secret="docs-key", header="X-Api-Key")
    async with httpx.AsyncClient(transport=vault_transport(inner, vault, header)) as client:
        await client.get("https://mcp.example.com/mcp")
    async with httpx.AsyncClient(transport=vault_transport(inner, vault, None)) as client:
        await client.get("https://mcp.example.com/mcp")
    assert received[0].headers["x-api-key"] == "k-1"
    assert "authorization" not in received[0].headers
    assert ("x-api-key" in received[1].headers, "authorization" in received[1].headers) == (
        False,
        False,
    )
    assert vault.reads == [("acme", "docs-key")]


async def test_vault_transport_refuses_other_origins() -> None:
    """发往清单地址之外的请求一律拒绝：凭据只会发给声明的那台主机。"""
    inner, received = recording_inner()
    vault = FakeVault({"docs-token": "secret-value"})
    transport = vault_transport(inner, vault, HttpAuth(type="bearer", secret="docs-token"))
    async with httpx.AsyncClient(transport=transport) as client:
        for url in ("https://evil.example.com/mcp", "http://mcp.example.com/mcp"):
            with pytest.raises(httpx.TransportError, match="清单"):
                await client.get(url)
    assert received == []
    assert vault.reads == []


async def test_vault_transport_missing_secret_and_close() -> None:
    """密钥取不到：请求不发出，缺密钥的错误照常上抛；关闭会关掉内层传输，重复关闭无害。"""
    closed: list[str] = []

    class Inner(httpx.AsyncBaseTransport):
        """记下关闭的内层传输。"""

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise AssertionError("不该发出请求")

        async def aclose(self) -> None:
            closed.append("inner")

    transport = vault_transport(Inner(), FakeVault({}), HttpAuth(type="bearer", secret="gone"))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SecretMissingError, match="gone"):
            await client.get("https://mcp.example.com/mcp")
    await transport.aclose()
    assert closed == ["inner"]
