"""HTTP 连接器：按 OpenAPI 把外部接口接成工具。外部接口是一个真的 HTTP 服务。"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from xuanpu_server.controlplane import AuditQuery
from xuanpu_server.errors import SecretMissingError
from xuanpu_server.runtime.http_api import build_request
from xuanpu_spec import pack_package, parse_openapi

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

BULK = 60


class ExternalApi:
    """被接入的外部系统：记下每次收到的请求。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.app = FastAPI()
        app = self.app

        @app.middleware("http")
        async def record(request: Request, call_next: Any) -> Any:
            body = await request.body()
            self.calls.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "raw_path": request.scope["raw_path"].decode("ascii"),
                    "query": dict(request.query_params),
                    "authorization": request.headers.get("authorization"),
                    "api_key": request.headers.get("x-api-key"),
                    "trace": request.headers.get("x-trace"),
                    "host_secret": request.headers.get("x-host-secret"),
                    "body": json.loads(body) if body else None,
                }
            )
            return await call_next(request)

        @app.get("/api/customers/{customer_id}")
        async def get_customer(customer_id: str, expand: str | None = None) -> dict[str, Any]:
            return {"id": customer_id, "name": "张三", "expand": expand}

        @app.get("/api/admin")
        async def admin() -> dict[str, str]:
            return {"secret": "管理接口不在 OpenAPI 文档里"}

        @app.post("/api/tickets", status_code=201)
        async def create_ticket(request: Request) -> dict[str, Any]:
            return {"ticket": "T-100", "received": await request.json()}

        @app.get("/api/redirect")
        async def redirect() -> RedirectResponse:
            return RedirectResponse("http://127.0.0.1:1/stolen", status_code=302)

        @app.get("/api/fail")
        async def fail() -> JSONResponse:
            return JSONResponse({"error": "库存系统不可用"}, status_code=503)

        @app.get("/api/big")
        async def big() -> PlainTextResponse:
            return PlainTextResponse("x" * (2 * 1024 * 1024))

        @app.get("/api/binary")
        async def binary() -> Response:
            return Response(b"\x00\x01\x02", media_type="application/octet-stream")

        @app.get("/api/bulk/{number}")
        async def bulk(number: int) -> dict[str, int]:
            return {"n": number}


def openapi_document() -> dict[str, Any]:
    """外部系统的 OpenAPI 文档：几条有代表性的接口，外加一大批同质接口。"""
    paths: dict[str, Any] = {
        "/customers/{id}": {
            "get": {
                "operationId": "getCustomer",
                "summary": "按编号取客户",
                "parameters": [
                    {"name": "id", "in": "path", "schema": {"type": "string"}},
                    {"name": "expand", "in": "query", "schema": {"type": "string"}},
                    {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                ],
            }
        },
        "/tickets": {
            "post": {
                "operationId": "createTicket",
                "summary": "新建工单",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {"type": "object"}}},
                },
            }
        },
        "/redirect": {"get": {"operationId": "followMe"}},
        "/fail": {"get": {"operationId": "alwaysFails"}},
        "/big": {"get": {"operationId": "hugeBody"}},
        "/binary": {"get": {"operationId": "binaryBody"}},
    }
    for number in range(BULK):
        paths[f"/bulk/{number}"] = {"get": {"operationId": f"bulk{number}"}}
    return {"openapi": "3.0.3", "info": {"title": "CRM"}, "paths": paths}


MANIFEST = """\
apiVersion: xuanpu/v1
metadata: {{name: crm-desk, version: 1.0.0}}
spec:
  skills: {{entries: [triage]}}
  policy: {{approval: {approval}}}
  connectors:
    - name: crm
      transport: http
      openapi: apis/crm.yaml
      baseUrl: {base_url}
      auth: {auth}
      timeoutSeconds: 5
"""

TRIAGE = """\
---
name: triage
description: 工单分诊
version: 1.0.0
type: composite
entry: true
child_skills: [faq]
tool_names: [{tools}]
---
# 工单分诊
"""

FAQ = "---\nname: faq\ndescription: 常见问题\nversion: 1.0.0\ntype: atomic\n---\n正文\n"

DEFAULT_TOOLS = (
    "api__crm__getCustomer, api__crm__createTicket, api__crm__followMe, "
    "api__crm__alwaysFails, api__crm__hugeBody, api__crm__binaryBody"
)


async def publish(
    world: Any,
    base_url: str,
    *,
    approval: str = "standard",
    auth: str = "{type: bearer, secret: crm-token}",
    tools: str = DEFAULT_TOOLS,
    name: str = "pkg",
) -> None:
    """发布一个带 HTTP 连接器的业务包。"""
    root: Path = world.package_dir / name
    (root / "skills" / "triage").mkdir(parents=True)
    (root / "skills" / "faq").mkdir(parents=True)
    (root / "apis").mkdir()
    (root / "xuanpu.yaml").write_text(
        MANIFEST.format(base_url=base_url + "/api", approval=approval, auth=auth), encoding="utf-8"
    )
    (root / "apis" / "crm.yaml").write_text(yaml.safe_dump(openapi_document()), encoding="utf-8")
    (root / "skills" / "triage" / "SKILL.md").write_text(
        TRIAGE.format(tools=tools), encoding="utf-8"
    )
    (root / "skills" / "faq" / "SKILL.md").write_text(FAQ, encoding="utf-8")
    await world.platform.packages.publish(world.admin, pack_package(root), activate=True)


@pytest.fixture
async def serve() -> AsyncIterator[Any]:
    """返回一个函数：把一个 ASGI 应用真的跑在本机端口上，返回它的地址。用例结束时全部停掉。"""
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


@pytest.fixture
async def external(serve: Any) -> tuple[ExternalApi, str]:
    """跑着的外部系统及其地址。"""
    api = ExternalApi()
    return api, await serve(api.app)


async def ask(
    world: Any, step: Any, read_turn: Any, calls: list[tuple[str, str, dict[str, Any]]]
) -> Any:
    """开一个会话，让模型发起给定的工具调用，返回事件。"""
    session = await world.platform.sessions.create(world.member, package="crm-desk")
    world.models.script(step(calls=calls), step(text="好"))
    turn = await world.platform.sessions.send_message(world.member, session.session_id, "办事")
    return session, await read_turn(turn)


def output_of(events: list[Any], call_id: str = "c1") -> dict[str, Any]:
    """某次工具调用的结果事件数据。"""
    return next(
        e.msg.data
        for e in events
        if e.msg.kind == "tool_call_completed" and e.msg.data["call_id"] == call_id
    )


async def test_read_operation_runs_with_injected_credentials(
    world: Any,
    step: Any,
    read_turn: Any,
    external: tuple[ExternalApi, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET 默认只读、直接放行；凭据由平台注入；参数各归其位；宿主环境与多余的头不会带出去。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    api, url = external
    await publish(world, url)
    _, events = await ask(
        world, step, read_turn,
        [("c1", "api__crm__getCustomer", {"id": "C-42 张", "expand": "orders", "X-Trace": "t-1"})],
    )  # fmt: skip
    result = output_of(events)
    assert result["is_error"] is False
    assert result["output"].startswith("HTTP 200\n")
    assert json.loads(result["output"].split("\n", 1)[1]) == {
        "id": "C-42 张", "name": "张三", "expand": "orders",
    }  # fmt: skip
    assert len(api.calls) == 1
    call = api.calls[0]
    assert (call["method"], call["path"], call["query"]) == (
        "GET", "/api/customers/C-42 张", {"expand": "orders"},
    )  # fmt: skip
    assert call["authorization"] == "Bearer s3cret-token-value"
    assert (call["api_key"], call["trace"], call["host_secret"], call["body"]) == (
        None, "t-1", None, None,
    )  # fmt: skip


async def test_path_parameter_cannot_change_the_route(
    world: Any, step: Any, read_turn: Any, external: tuple[ExternalApi, str]
) -> None:
    """路径参数整体转义：想借 ``../`` 打到文档之外的接口，发出去的是转义后的一段，打不到。"""
    api, url = external
    await publish(world, url)
    _, events = await ask(
        world, step, read_turn, [("c1", "api__crm__getCustomer", {"id": "../admin"})]
    )
    assert [call["raw_path"] for call in api.calls] == ["/api/customers/..%2Fadmin"]
    assert "管理接口" not in output_of(events)["output"]


async def test_write_operation_needs_approval(
    world: Any, step: Any, read_turn: Any, external: tuple[ExternalApi, str]
) -> None:
    """POST 按连接器默认是对外副作用：挂起等审批，批准后才真的发请求。"""
    api, url = external
    await publish(world, url)
    session, events = await ask(
        world, step, read_turn,
        [("c1", "api__crm__createTicket", {"body": {"title": "打印机坏了", "level": 2}})],
    )  # fmt: skip
    assert events[-1].msg.kind == "turn_suspended"
    assert api.calls == []
    pending = events[-1].msg.data["pending"][0]
    assert pending["detail"]["target"] == "api__crm__createTicket"

    resumed = await read_turn(
        await world.platform.sessions.resume(
            world.member, session.session_id, {pending["request_id"]: {"granted": True}}
        )
    )
    assert resumed[-1].msg.kind == "turn_completed"
    assert [(c["method"], c["path"], c["body"]) for c in api.calls] == [
        ("POST", "/api/tickets", {"title": "打印机坏了", "level": 2})
    ]

    gate = await world.platform.store.query_audit("acme", AuditQuery(source="gate"))
    assert [
        (r.data["transport"], r.data["tool"], r.data["effect"], r.data["decision"]) for r in gate
    ] == [("http", "createTicket", "external", "allowed")]
    assert gate[0].session_id == session.session_id
    assert "打印机" not in json.dumps(gate[0].data, ensure_ascii=False)
    everything = json.dumps(
        [r.data for r in await world.platform.store.query_audit("acme", AuditQuery(limit=1000))],
        ensure_ascii=False,
    )
    assert "s3cret-token-value" not in everything


@pytest.mark.parametrize(
    ("auth", "secret", "expected"),
    [
        ("{type: header, secret: crm-token, header: X-Api-Key}", "k-1", {"api_key": "k-1"}),
        ("{type: basic, secret: crm-token}", "user:pass", {"authorization": "Basic dXNlcjpwYXNz"}),
        ("{type: query, secret: crm-token, name: api_key}", "k-2", {"query": {"api_key": "k-2"}}),
    ],
)
async def test_auth_styles(
    world: Any,
    step: Any,
    read_turn: Any,
    external: tuple[ExternalApi, str],
    auth: str,
    secret: str,
    expected: dict[str, Any],
) -> None:
    """四种凭据注入方式各自落到该去的位置；放在查询参数里的凭据不会出现在给模型的结果里。"""
    api, url = external
    await world.platform.put_secret(world.admin, "crm-token", secret)
    await publish(world, url, auth=auth)
    _, events = await ask(world, step, read_turn, [("c1", "api__crm__getCustomer", {"id": "1"})])
    assert output_of(events)["is_error"] is False
    for key, value in expected.items():
        assert api.calls[0][key] == value
    assert secret not in output_of(events)["output"]
    assert secret not in world.models.last_prompt()


async def test_no_auth_connector_sends_no_credentials(
    world: Any, step: Any, read_turn: Any, external: tuple[ExternalApi, str]
) -> None:
    """不声明凭据的连接器不带任何凭据头。"""
    api, url = external
    await publish(world, url, auth="null")
    await ask(world, step, read_turn, [("c1", "api__crm__getCustomer", {"id": "1"})])
    assert (api.calls[0]["authorization"], api.calls[0]["api_key"]) == (None, None)


async def test_missing_secret_blocks_the_runtime(
    world: Any, step: Any, external: tuple[ExternalApi, str]
) -> None:
    """凭据没配：对话起不来，并且说清楚缺哪条（与 stdio 连接器一致）。"""
    api, url = external
    await publish(world, url)
    await world.platform.delete_secret(world.admin, "crm-token")
    session = await world.platform.sessions.create(world.member, package="crm-desk")
    with pytest.raises(SecretMissingError, match="crm-token"):
        await world.platform.sessions.send_message(world.member, session.session_id, "办事")
    assert api.calls == []


async def test_secret_removed_mid_session_fails_the_call_not_the_turn(
    world: Any, step: Any, read_turn: Any, external: tuple[ExternalApi, str]
) -> None:
    """运行时已经组装好之后密钥被删：这次调用记为失败并进审计，对话本身继续。"""
    api, url = external
    await publish(world, url)
    session = await world.platform.sessions.create(world.member, package="crm-desk")
    world.models.script(step(text="先聊两句"))
    await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "你好")
    )
    await world.platform.delete_secret(world.admin, "crm-token")

    world.models.script(
        step(calls=[("c1", "api__crm__getCustomer", {"id": "1"})]), step(text="查不了")
    )
    events = await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "查客户")
    )
    assert events[-1].msg.kind == "turn_completed"
    result = output_of(events)
    assert result["is_error"] is True
    assert result["output"].startswith("secret_missing")
    assert api.calls == []
    gate = await world.platform.store.query_audit("acme", AuditQuery(source="gate"))
    assert [(r.data["is_error"], r.data["detail"]) for r in gate] == [(True, "secret_missing")]


async def test_redirects_errors_and_large_or_binary_bodies(
    world: Any, step: Any, read_turn: Any, external: tuple[ExternalApi, str]
) -> None:
    """重定向不跟随；4xx / 5xx 连同响应体交给模型并记为出错；超大与二进制响应体不会原样塞给模型。"""
    api, url = external
    await publish(world, url)
    _, events = await ask(
        world, step, read_turn,
        [
            ("c1", "api__crm__followMe", {}),
            ("c2", "api__crm__alwaysFails", {}),
            ("c3", "api__crm__hugeBody", {}),
            ("c4", "api__crm__binaryBody", {}),
        ],
    )  # fmt: skip
    redirect = output_of(events, "c1")
    assert redirect["output"] == "HTTP 302（重定向未跟随）"
    assert all(call["path"] != "/stolen" for call in api.calls)

    failure = output_of(events, "c2")
    assert failure["is_error"] is True
    assert failure["output"].startswith("HTTP 503\n")
    assert "库存系统不可用" in failure["output"]

    huge = output_of(events, "c3")
    assert len(huge["output"]) < 100_000
    assert "已截断" in huge["output"]

    binary = output_of(events, "c4")
    assert binary["output"] == "HTTP 200\n[application/octet-stream，3 字节，未展示]"


async def test_undeclared_arguments_are_rejected_before_any_request(
    world: Any, step: Any, read_turn: Any, external: tuple[ExternalApi, str]
) -> None:
    """模型给了 operation 没声明的参数（比如想自带一个头）：不发请求，直接报错。"""
    api, url = external
    await publish(world, url)
    _, events = await ask(
        world, step, read_turn,
        [("c1", "api__crm__getCustomer", {"id": "1", "Authorization": "Bearer forged"})],
    )  # fmt: skip
    result = output_of(events)
    assert result["is_error"] is True
    assert "Authorization" in result["output"]
    assert api.calls == []


async def test_unreachable_api_reports_type_only(world: Any, step: Any, read_turn: Any) -> None:
    """外部系统连不上：报错里只有异常类型，没有 URL 与凭据。"""
    await publish(world, "http://127.0.0.1:1")
    _, events = await ask(world, step, read_turn, [("c1", "api__crm__getCustomer", {"id": "1"})])
    result = output_of(events)
    assert result["is_error"] is True
    assert "ConnectError" in result["output"]
    assert "127.0.0.1" not in result["output"]
    assert "s3cret" not in result["output"]


async def test_large_api_surface_only_exposes_what_the_skill_names(
    world: Any, step: Any, read_turn: Any, external: tuple[ExternalApi, str]
) -> None:
    """文档里有六十多个接口，skill 只点名两个：模型只看到这两个，其余的调不到。"""
    api, url = external
    await publish(world, url, tools="api__crm__bulk7, api__crm__bulk42")
    runtime_tools = None
    _, events = await ask(
        world, step, read_turn,
        [("c1", "api__crm__bulk42", {}), ("c2", "api__crm__bulk3", {})],
    )  # fmt: skip
    offered = {tool["name"] for tool in world.models.requests[0].model_dump()["tools"]}
    assert {name for name in offered if name.startswith("api__")} == {
        "api__crm__bulk7", "api__crm__bulk42",
    }  # fmt: skip
    assert output_of(events, "c1")["output"] == 'HTTP 200\n{"n": 42}'
    assert output_of(events, "c2")["is_error"] is True
    assert [call["path"] for call in api.calls] == ["/api/bulk/42"]
    runtime_tools = next(iter(world.platform.runtimes._runtimes.values())).tool_effects  # noqa: SLF001
    assert len(runtime_tools) == BULK + 6
    assert runtime_tools["api__crm__bulk7"] == "read"
    assert runtime_tools["api__crm__createTicket"] == "external"


def test_build_request_unit() -> None:
    """请求拼装：路径参数转义、数组查询参数展开、布尔按 JSON 写法、必填检查。"""
    document = parse_openapi(
        yaml.safe_dump(
            {
                "openapi": "3.0.0",
                "paths": {
                    "/items/{id}": {
                        "put": {
                            "operationId": "update",
                            "parameters": [
                                {"name": "id", "in": "path"},
                                {"name": "tag", "in": "query", "schema": {"type": "array"}},
                                {"name": "dry", "in": "query", "schema": {"type": "boolean"}},
                                {"name": "must", "in": "query", "required": True},
                            ],
                            "requestBody": {
                                "required": True,
                                "content": {"application/json": {"schema": {"type": "object"}}},
                            },
                        }
                    }
                },
            }
        )
    )
    operation = document.operations["update"]
    method, url, query, headers, body = build_request(
        "https://x/api",
        operation,
        {"id": "a/b?c", "tag": ["x", "y"], "dry": True, "must": 1, "body": {"k": "v"}},
    )
    assert (method, url) == ("PUT", "https://x/api/items/a%2Fb%3Fc")
    assert query == [("tag", "x"), ("tag", "y"), ("dry", "true"), ("must", "1")]
    assert (headers, body) == ({}, {"k": "v"})
    with pytest.raises(ValueError, match="must"):
        build_request("https://x", operation, {"id": "1", "body": {}})
    with pytest.raises(ValueError, match="body"):
        build_request("https://x", operation, {"id": "1", "must": 1})
    with pytest.raises(ValueError, match="未声明"):
        build_request("https://x", operation, {"id": "1", "must": 1, "body": {}, "extra": 1})
