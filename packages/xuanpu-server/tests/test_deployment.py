"""部署形态的端到端测试：按配置装配平台，全部换成真实组件。

- 控制面与状态面落真实的 Postgres；
- 模型经内核自带的 OpenAI Chat 客户端访问一个本地起的、兼容该接口的假网关；
- 请求经真实的 HTTP 服务器进出。
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import socket
from typing import TYPE_CHECKING, Any

import asyncpg
import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from taifeng_stores import SqlNamespace
from xuanpu_server.api import create_app
from xuanpu_server.bootstrap import open_platform, tenant_table_prefix
from xuanpu_server.config import parse_config
from xuanpu_server.controlplane.schema import apply_migrations
from xuanpu_server.errors import SchemaError
from xuanpu_server.tenancy import generate_root_key
from xuanpu_spec import pack_package

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

MANIFEST = """\
apiVersion: xuanpu/v1
metadata: {name: helpdesk, version: 1.0.0}
spec:
  skills: {entries: [main]}
  model: {tier: fast}
  policy: {approval: relaxed}
"""
MAIN = """\
---
name: main
description: 入口
version: 1.0.0
type: composite
entry: true
model: some-model-the-author-liked
child_skills: [notes]
---
# 入口
简短作答。
"""
NOTES = "---\nname: notes\ndescription: 备注\nversion: 1.0.0\ntype: atomic\n---\n简短。\n"


def free_port() -> int:
    """取一个空闲端口。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Served:
    """在测试进程里跑着的一个 HTTP 服务。"""

    def __init__(self, app: Any) -> None:
        self.port = free_port()
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self._task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        """服务地址。"""
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        """启动并等到可以接受连接。"""
        self._task = asyncio.ensure_future(self._server.serve())
        for _ in range(200):
            if self._server.started:
                return
            await asyncio.sleep(0.02)
        raise RuntimeError("HTTP 服务没有启动")

    async def stop(self) -> None:
        """停止。"""
        self._server.should_exit = True
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=10)


class FakeGateway:
    """兼容 OpenAI Chat 接口的假模型网关：记下每次请求，按脚本回复。"""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.authorizations: list[str | None] = []
        self.replies: list[str] = []
        self.app = FastAPI()
        self.app.post("/v1/chat/completions")(self._complete)

    async def _complete(self, request: Request) -> StreamingResponse:
        payload = await request.json()
        self.requests.append(payload)
        self.authorizations.append(request.headers.get("authorization"))
        reply = self.replies.pop(0) if self.replies else "（没有脚本）"

        def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
            body = {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "model": payload["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"

        async def frames() -> AsyncIterator[str]:
            yield chunk({"role": "assistant", "content": ""})
            for piece in (reply[: len(reply) // 2], reply[len(reply) // 2 :]):
                yield chunk({"content": piece})
            yield chunk({}, finish="stop")
            usage = {"prompt_tokens": 40, "completion_tokens": 8, "total_tokens": 48}
            yield (
                "data: "
                + json.dumps(
                    {"id": "cmpl-1", "model": payload["model"], "choices": [], "usage": usage}
                )
                + "\n\n"
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 响应体拆成 ``(事件名, 数据)``。"""
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in body.strip().split("\n\n"):
        name, data = "", {}
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if name:
            frames.append((name, data))
    return frames


@pytest.fixture
async def gateway() -> AsyncIterator[tuple[FakeGateway, str]]:
    """跑着的假网关及其地址。"""
    fake = FakeGateway()
    served = Served(fake.app)
    await served.start()
    try:
        yield fake, served.url + "/v1"
    finally:
        await served.stop()


@pytest.fixture
async def schema(postgres_dsn: str) -> AsyncIterator[str]:
    """独占的 schema；用例结束时整个删掉。"""
    name = "t_" + secrets.token_hex(6)
    try:
        yield name
    finally:
        conn = await asyncpg.connect(postgres_dsn)
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        finally:
            await conn.close()


def make_config(
    tmp_path: Path, dsn: str, schema: str, gateway_url: str, root_hash: str, instance: str
) -> Any:
    """一份指向真实后端的配置。每个实例一个自己的数据目录（以实例标识命名），对象存储共用。"""
    postgres = {"dsn": "${PG_DSN}", "schema": schema}
    document = {
        "data_dir": str((tmp_path / f"data-{instance}").resolve()),
        "object_store": {
            "backend": "local",
            "local": {"root": str((tmp_path / "objects").resolve())},
        },
        "root_key_hash": root_hash,
        "master_keys": {"k1": base64.b64encode(b"m" * 32).decode()},
        "primary_master_key": "k1",
        "instance_id": instance,
        "models": {
            "base_url": gateway_url,
            "api_key_secret": "gateway-key",
            "tiers": {
                "fast": {"model": "small-model"},
                "balanced": {"model": "medium-model"},
                "deep": {"model": "large-model"},
            },
        },
        "control_plane": {"backend": "postgres", "postgres": postgres},
        "state": {"backend": "postgres", "postgres": postgres},
    }
    return parse_config(json.dumps(document), variables={"PG_DSN": dsn})


def package_archive(tmp_path: Path) -> bytes:
    """打一个最小的业务包。"""
    root = tmp_path / "helpdesk"
    (root / "skills" / "main").mkdir(parents=True)
    (root / "skills" / "notes").mkdir(parents=True)
    (root / "xuanpu.yaml").write_text(MANIFEST, encoding="utf-8")
    (root / "skills" / "main" / "SKILL.md").write_text(MAIN, encoding="utf-8")
    (root / "skills" / "notes" / "SKILL.md").write_text(NOTES, encoding="utf-8")
    return pack_package(root)


async def test_unmigrated_database_fails_at_startup(
    tmp_path: Path, postgres_dsn: str, schema: str
) -> None:
    """控制面的表没迁移：装配平台时就报错，而不是等到第一个请求。"""
    config = make_config(tmp_path, postgres_dsn, schema, "http://unused/v1", "a" * 64, "i1")
    with pytest.raises(SchemaError, match="xuanpu migrate"):
        async with open_platform(config):
            pass


async def test_real_deployment_end_to_end(
    tmp_path: Path, postgres_dsn: str, schema: str, gateway: tuple[FakeGateway, str]
) -> None:
    """真实部署形态下走完整流程，再让第二个实例（数据目录不同）从库里接手同一个会话。"""
    fake, gateway_url = gateway
    root_key, root_hash = generate_root_key()
    pool = await asyncpg.create_pool(postgres_dsn, min_size=1, max_size=2)
    try:
        await apply_migrations(pool, SqlNamespace(schema=schema, prefix="xp_"))

        config = make_config(tmp_path, postgres_dsn, schema, gateway_url, root_hash, "inst-a")
        async with open_platform(config) as platform:
            served = Served(create_app(platform))
            await served.start()
            try:
                session_id = await _drive_first_instance(
                    served.url, root_key, package_archive(tmp_path), fake
                )
            finally:
                await served.stop()

        await _check_database(pool, schema, session_id)

        # 第二个实例：同一份数据库与对象存储、另一个数据目录，接手同一个会话
        second = make_config(tmp_path, postgres_dsn, schema, gateway_url, root_hash, "inst-b")
        async with open_platform(second) as platform:
            served = Served(create_app(platform))
            await served.start()
            try:
                await _drive_second_instance(served.url, root_key, session_id, fake)
            finally:
                await served.stop()
        # 会话主存在库里：两个实例的数据目录里都没有会话文件
        assert not (tmp_path / "data-inst-a" / "threads").exists()
        assert not (tmp_path / "data-inst-b" / "threads").exists()
    finally:
        await pool.close()


async def _tenant_admin(http: httpx.AsyncClient, root_key: str) -> dict[str, str]:
    """用根凭据给租户 acme 签发一把管理员凭据。"""
    root = {"Authorization": f"Bearer {root_key}"}
    issued = await http.post(
        "/v1/admin/tenants/acme/keys", headers=root, json={"name": "a", "role": "tenant_admin"}
    )
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['key']}"}


async def _drive_first_instance(url: str, root_key: str, archive: bytes, fake: FakeGateway) -> str:
    """第一个实例：开通租户、发布业务包、对话一轮。返回会话标识。"""
    async with httpx.AsyncClient(base_url=url, timeout=30) as http:
        assert (await http.get("/healthz")).json()["instance"] == "inst-a"
        root = {"Authorization": f"Bearer {root_key}"}
        created = await http.post(
            "/v1/admin/tenants", headers=root, json={"tenant_id": "acme", "name": "Acme"}
        )
        assert created.status_code == 201, created.text
        admin = await _tenant_admin(http, root_key)
        await http.put("/v1/secrets/gateway-key", headers=admin, json={"value": "sk-acme-tenant"})
        published = await http.post("/v1/packages?activate=true", headers=admin, content=archive)
        assert published.status_code == 201, published.text

        session = await http.post("/v1/sessions", headers=admin, json={"package": "helpdesk"})
        session_id = str(session.json()["session_id"])
        fake.replies.append("订单号是 A-1024")
        reply = await http.post(
            f"/v1/sessions/{session_id}/messages", headers=admin, json={"text": "查一下订单"}
        )
        frames = parse_sse(reply.text)
        assert [name for name, _ in frames][-2:] == ["turn_completed", "end"]
        text = "".join(d["data"]["delta"] for n, d in frames if n == "assistant_text")
        assert text == "订单号是 A-1024"

    # 发到网关的是平台按档位选的模型，不是 skill 里随手写的那个；凭据是这个租户自己的
    assert [request["model"] for request in fake.requests] == ["small-model"]
    assert fake.authorizations == ["Bearer sk-acme-tenant"]
    assert fake.requests[0]["messages"][-1] == {"role": "user", "content": "查一下订单"}
    return session_id


async def _check_database(pool: asyncpg.Pool, schema: str, session_id: str) -> None:
    """数据都落进了库里：控制面的表，以及该租户专属前缀下的线程目录与会话主存。"""
    prefix = tenant_table_prefix("acme")
    async with pool.acquire() as conn:
        tables = {
            row["table_name"]
            for row in await conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = $1",
                schema,
            )
        }
        assert {"xp_tenant", "xp_session", "xp_audit", "xp_usage"} <= tables
        assert {f"{prefix}thread", f"{prefix}message"} <= tables

        session = await conn.fetchrow(
            f'SELECT thread_id, owner, status FROM "{schema}".xp_session WHERE session_id = $1',
            session_id,
        )
        assert session is not None
        # 实例退出时放弃了持有权，会话保持活动
        assert (session["owner"], session["status"]) == (None, "active")

        # 主存是同步写的：一轮结束时消息已经在库里，每条只有一份
        stored = await conn.fetch(
            f'SELECT kind, payload->>\'text\' AS text FROM "{schema}"."{prefix}message" '
            "WHERE thread_id = $1 ORDER BY position, seq",
            session["thread_id"],
        )
        assert [(row["kind"], row["text"]) for row in stored] == [
            ("user_message", "查一下订单"),
            ("assistant_message", "订单号是 A-1024"),
        ]
        usage = await conn.fetchrow(f'SELECT * FROM "{schema}".xp_usage')
        assert usage is not None
        assert (usage["input_tokens"], usage["output_tokens"], usage["turns"]) == (40, 8, 1)
        secret = await conn.fetchval(f'SELECT ciphertext FROM "{schema}".xp_secret')
        assert b"sk-acme-tenant" not in bytes(secret)


async def _drive_second_instance(
    url: str, root_key: str, session_id: str, fake: FakeGateway
) -> None:
    """第二个实例接手会话：上下文还在，审计里两个实例的记录接得上。"""
    async with httpx.AsyncClient(base_url=url, timeout=30) as http:
        assert (await http.get("/healthz")).json()["instance"] == "inst-b"
        admin = await _tenant_admin(http, root_key)
        fake.replies.append("是 A-1024")
        reply = await http.post(
            f"/v1/sessions/{session_id}/messages", headers=admin, json={"text": "刚才的订单号？"}
        )
        assert [name for name, _ in parse_sse(reply.text)][-2:] == ["turn_completed", "end"]
        contents = [message["content"] for message in fake.requests[-1]["messages"]]
        assert "订单号是 A-1024" in contents
        assert contents[-1] == "刚才的订单号？"

        current = await http.get(f"/v1/sessions/{session_id}", headers=admin)
        assert current.json()["owner"] == "inst-b"
        audit = await http.get(
            "/v1/audit",
            headers=admin,
            params={"session_id": session_id, "kind": "turn_completed"},
        )
        records = audit.json()["records"]
        assert len(records) == 2
        assert len({record["stream"] for record in records}) == 2
        usage = await http.get(
            "/v1/usage", headers=admin, params={"since": "2000-01-01", "until": "2999-12-31"}
        )
        assert [(item["turns"], item["total_tokens"]) for item in usage.json()["usage"]] == [
            (2, 96)
        ]
