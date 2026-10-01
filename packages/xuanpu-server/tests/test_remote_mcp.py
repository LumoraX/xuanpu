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

import pytest
import taifeng
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from xuanpu_server.controlplane import AuditQuery
from xuanpu_server.errors import RuntimeUnavailableError, SecretMissingError
from xuanpu_server.runtime.connector_process import KernelMcpLogGuard
from xuanpu_server.runtime.remote_mcp import RemoteRun, VaultAuthTransport
from xuanpu_spec import pack_package

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from xuanpu_server.runtime import TenantRuntime

TOKEN = "tok-123"
SESSION = "sess-1"

# 清单白名单里的工具与副作用分类。search 回显参数；update_doc 是写操作；fail 回 isError 并回显
# 凭据；slow 等到放行才返回；bad_code / null_code / not_utf8 回内核解析不了的响应（见 _malformed）；
# sse_noise 回 SSE 流，先夹一条回显凭据、解析不了的 data；whoami 成功返回、结果里回显凭据
WHITELIST: dict[str, str] = {
    "search": "read",
    "update_doc": "write",
    "fail": "read",
    "slow": "read",
    "bad_code": "read",
    "null_code": "read",
    "not_utf8": "read",
    "sse_noise": "read",
    "whoami": "read",
}

# 远端提供的工具：白名单里的全部，外加不在白名单里的 admin_purge
TOOLS: list[dict[str, Any]] = [
    {"name": name, "description": f"{name} 工具", "inputSchema": {"type": "object"}}
    for name in [*WHITELIST, "admin_purge"]
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
    """最小的 MCP Streamable HTTP 服务。本身就是 ASGI 应用。

    ``sse=True`` 时 JSON-RPC 响应都以 SSE 流（``text/event-stream``，一个 ``event: message``
    事件）回，官方 SDK 的服务端默认就是这样；否则回 ``application/json``。
    """

    def __init__(self, *, sse: bool = False) -> None:
        self.state = RemoteState()
        self.sse = sse
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
            return self._reply(
                {"jsonrpc": "2.0", "id": message["id"], "result": result},
                headers={"Mcp-Session-Id": SESSION},
            )
        if request.headers.get("mcp-session-id") != SESSION:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        if method == "tools/list":
            return self._reply({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": TOOLS}})
        if method == "tools/call":
            malformed = _malformed(request, message)
            if malformed is not None:
                return malformed
            result = await self._call(request, message["params"])
            return self._reply({"jsonrpc": "2.0", "id": message["id"], "result": result})
        error = {"code": -32601, "message": f"method not found: {method}"}
        return self._reply({"jsonrpc": "2.0", "id": message["id"], "error": error})

    def _reply(self, payload: dict[str, Any], headers: dict[str, str] | None = None) -> Response:
        """一条 JSON-RPC 响应：按模式回 JSON 或 SSE。"""
        if self.sse:
            response = _sse(json.dumps(payload, ensure_ascii=False))
            response.headers.update(headers or {})
            return response
        return JSONResponse(payload, headers=headers)

    async def _call(self, request: Request, params: dict[str, Any]) -> dict[str, Any]:
        """执行一个工具。``fail`` 回 isError 并回显凭据；``slow`` 等到放行。"""
        name, arguments = params["name"], params.get("arguments", {})
        if name == "slow":
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.state.release.wait(), timeout=10)
        if name == "fail":
            text = f"后端拒绝了 {request.headers.get('authorization')}"
            return {"isError": True, "content": [{"type": "text", "text": text}]}
        if name == "whoami":
            text = f"你用的凭据是 {request.headers.get('authorization')}"
            return {"content": [{"type": "text", "text": text}]}
        text = f"{name}: {json.dumps(arguments, ensure_ascii=False)}"
        return {"content": [{"type": "text", "text": text}]}


def _malformed(request: Request, message: dict[str, Any]) -> Response | None:
    """内核解析不了的几种响应，都回显收到的 ``Authorization`` 头；别的工具返回 None。

    ``bad_code``：``error.code`` 是字符串（就是那个头）；``null_code``：``error.code`` 是 null；
    ``not_utf8``：响应体不是合法的 UTF-8。
    """
    name = message["params"]["name"]
    echoed = request.headers.get("authorization") or ""
    if name == "bad_code":
        error: dict[str, Any] = {"code": echoed, "message": "x"}
        return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "error": error})
    if name == "null_code":
        error = {"code": None, "message": echoed}
        return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "error": error})
    if name == "not_utf8":
        body = f'{{"jsonrpc": "2.0", "id": {message["id"]}, "result": "'.encode()
        return Response(body + b"\xff " + echoed.encode() + b'"}', media_type="application/json")
    if name == "sse_noise":
        result = {"content": [{"type": "text", "text": "噪声之后的结果"}]}
        response = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        return _sse(f"not-json {echoed}", json.dumps(response, ensure_ascii=False))
    return None


def _sse(*events: str) -> Response:
    """SSE 响应：每段文本是一个 ``event: message`` 事件的 data。"""
    body = "".join(f"event: message\ndata: {data}\n\n" for data in events)
    return Response(body, media_type="text/event-stream")


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
      tools:{tools}{extra_tools}
"""

TOOL_LINES = "".join(
    f"\n        - {{name: {name}, effect: {effect}}}" for name, effect in WHITELIST.items()
)

HELPER = """\
---
name: helper
description: 文档助手
version: 1.0.0
type: composite
entry: true
child_skills: [faq]
tool_names: [{tools}]
---
# 文档助手
""".format(tools=", ".join(f"mcp__docs__{name}" for name in WHITELIST))

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
        MANIFEST.format(
            url=url, auth=auth, timeout=timeout, tools=TOOL_LINES, extra_tools=extra_tools
        ),
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


@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
async def test_remote_mcp_tools_go_through_the_gate(
    world: Any, serve: Any, step: Any, read_turn: Any, sse: bool
) -> None:
    """远端 MCP 的工具与 stdio 一样过闸：读操作放行并留过闸记录；凭据按名字注入。

    远端回 JSON 与回 SSE 流（官方 SDK 服务端的默认做法）两种各走一遍。
    """
    fake_remote_mcp = FakeRemoteMcp(sse=sse)
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
        f"mcp__docs__{name}": effect for name, effect in WHITELIST.items()
    }
    assert effects["mcp__docs__update_doc"] == "write"
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


async def test_rejected_remote_write_leaves_a_gate_record(
    world: Any, serve: Any, step: Any, read_turn: Any, fake_remote_mcp: FakeRemoteMcp
) -> None:
    """写操作被人工拒绝：远端没收到调用；补一条过闸记录，传输按连接器登记是 remote。

    remote 与 stdio 的工具名前缀都是 ``mcp__``，传输不能从前缀推。
    """
    session_id = await ready(world, serve, fake_remote_mcp)
    arguments = {"id": "D-1", "text": "新内容"}
    events = await ask(
        world, step, read_turn, session_id, [("c1", "mcp__docs__update_doc", arguments)]
    )
    pending = events[-1].msg.data["pending"][0]
    resumed = await read_turn(
        await world.platform.sessions.resume(
            world.member, session_id, {pending["request_id"]: {"granted": False}}
        )
    )
    assert resumed[-1].msg.kind == "turn_completed"
    assert fake_remote_mcp.state.rpc("tools/call") == []
    gate = await gate_records(world)
    assert [
        (r.data["transport"], r.data["connector"], r.data["tool"], r.data["effect"]) for r in gate
    ] == [("remote", "docs", "update_doc", "write")]
    assert (gate[0].data["decision"], gate[0].data["detail"]) == ("rejected", "rejected_by_user")
    assert gate[0].data["submission_id"] == events[-1].submission_id
    assert gate[0].session_id == session_id
    assert "新内容" not in json.dumps(gate[0].data, ensure_ascii=False)


@pytest.mark.parametrize(
    ("tool", "kind"),
    [("bad_code", "ValueError"), ("null_code", "TypeError"), ("not_utf8", "UnicodeDecodeError")],
)
async def test_malformed_remote_responses_still_pass_the_gate(
    world: Any,
    serve: Any,
    step: Any,
    read_turn: Any,
    fake_remote_mcp: FakeRemoteMcp,
    caplog: pytest.LogCaptureFixture,
    tool: str,
    kind: str,
) -> None:
    """远端回了内核解析不了的响应（error.code 是回显凭据的字符串或 null、响应体不是 UTF-8）。

    内核解析时冒出的异常不在预料之中：调用照样记为失败、留下一条过闸记录，交给模型的只有
    异常类型；模型的提示词、全部审计与日志里都没有明文。
    """
    caplog.set_level(logging.DEBUG)
    session_id = await ready(world, serve, fake_remote_mcp)
    events = await ask(world, step, read_turn, session_id, [("c1", f"mcp__docs__{tool}", {})])
    assert events[-1].msg.kind == "turn_completed"
    result = output_of(events)
    assert result["is_error"] is True
    assert result["output"] == f"connector_error: 连接器的响应无法处理：{kind}"
    gate = await gate_records(world)
    assert [(r.data["tool"], r.data["decision"], r.data["is_error"]) for r in gate] == [
        (tool, "allowed", True)
    ]
    assert TOKEN not in world.models.last_prompt()
    assert TOKEN not in await everything(world)
    assert TOKEN not in caplog.text


async def test_kernel_logs_of_remote_sse_noise_keep_no_plaintext(
    world: Any,
    serve: Any,
    step: Any,
    read_turn: Any,
    fake_remote_mcp: FakeRemoteMcp,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """远端的 SSE 流里夹了一条解析不了、回显凭据的 data：内核照常跳过它并记日志，日志里只有
    长度没有内容；后面的响应照常交给模型。"""
    caplog.set_level(logging.DEBUG)
    session_id = await ready(world, serve, fake_remote_mcp)
    events = await ask(world, step, read_turn, session_id, [("c1", "mcp__docs__sse_noise", {})])
    assert output_of(events)["output"] == "噪声之后的结果"
    [noise] = [r for r in caplog.records if "invalid SSE JSON" in r.getMessage()]
    assert noise.getMessage().endswith("内容不记>")
    assert TOKEN not in caplog.text


async def test_unexpected_exception_reaching_the_gate_leaves_a_record(
    world: Any,
    serve: Any,
    step: Any,
    read_turn: Any,
    fake_remote_mcp: FakeRemoteMcp,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调用实现里的异常一路冒到闸门（这里直接换掉调用实现）：闸门兜底。

    过闸记录照留，detail 与交给模型的错误都只有异常类型，异常文本（带着凭据）哪里都不出现。
    """

    def invoker(self: RemoteRun, tool: str) -> Any:
        """一律抛出文本里带凭据的 ValueError。"""

        async def invoke(args: dict[str, Any]) -> tuple[str, bool]:
            raise ValueError(f"坏了 Bearer {TOKEN}")

        return invoke

    monkeypatch.setattr(RemoteRun, "invoker", invoker)
    caplog.set_level(logging.DEBUG)
    session_id = await ready(world, serve, fake_remote_mcp)
    events = await ask(world, step, read_turn, session_id, [("c1", "mcp__docs__search", {})])
    assert events[-1].msg.kind == "turn_completed"
    assert output_of(events)["output"] == "connector_error: ValueError"
    gate = await gate_records(world)
    assert [(r.data["tool"], r.data["is_error"], r.data["detail"]) for r in gate] == [
        ("search", True, "ValueError")
    ]
    assert TOKEN not in world.models.last_prompt()
    assert TOKEN not in await everything(world)
    assert TOKEN not in caplog.text


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


async def test_successful_result_echoing_the_secret_is_redacted(
    world: Any, serve: Any, step: Any, read_turn: Any, fake_remote_mcp: FakeRemoteMcp
) -> None:
    """远端成功返回、结果里却回显了凭据：交给模型的结果同样按值脱敏，调用不算出错。"""
    session_id = await ready(world, serve, fake_remote_mcp)
    events = await ask(world, step, read_turn, session_id, [("c1", "mcp__docs__whoami", {})])
    result = output_of(events)
    assert (result["is_error"], result["output"]) == (False, "你用的凭据是 Bearer ***")
    assert TOKEN not in world.models.last_prompt()


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
