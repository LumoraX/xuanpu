"""平台服务测试的公共夹具。

工作区用 importlib 导入模式，测试模块之间不能互相 import，所以共用的东西都经夹具提供。

Postgres 相关用例对真实数据库执行：设置 ``XUANPU_TEST_PG_DSN`` 使用已有服务（须装有
pgvector 扩展），否则用 Docker 临时拉起一个；两者都没有时跳过。每个用例独占一个 schema。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import asyncpg
import pytest
from taifeng import ResponseEvent, TokenUsage
from taifeng_stores import SqlNamespace
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.controlplane.postgres import PostgresControlPlane
from xuanpu_server.controlplane.schema import apply_migrations
from xuanpu_server.platform import ROOT_PRINCIPAL, Platform
from xuanpu_server.runtime import DockerSandboxProvider, LocalSandboxProvider
from xuanpu_server.secrets import EncryptedVault, generate_master_key
from xuanpu_server.tenancy import generate_root_key
from xuanpu_spec import pack_package

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    import taifeng
    from xuanpu_server.controlplane import ControlPlaneStore
    from xuanpu_server.tenancy import Principal

PG_IMAGE = "pgvector/pgvector:pg16"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """运行 docker 命令。"""
    docker = shutil.which("docker")
    assert docker is not None
    return subprocess.run(  # noqa: S603 —— 参数全部来自测试代码
        [docker, *args], capture_output=True, text=True, timeout=300, check=check
    )


def _docker_available() -> bool:
    """本机是否有可用的 Docker 守护进程。"""
    if shutil.which("docker") is None:
        return False
    try:
        return _docker("info", "--format", "{{.ServerVersion}}", check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _start_postgres() -> tuple[str, str | None] | None:
    """取得一个 Postgres，返回 ``(连接串, 临时容器名)``；拿不到返回 None。"""
    dsn = os.environ.get("XUANPU_TEST_PG_DSN")
    if dsn:
        return dsn, None
    if not _docker_available():
        return None
    name = "xuanpu-pg-" + secrets.token_hex(4)
    _docker(
        "run", "--detach", "--rm", "--name", name,
        "--env", "POSTGRES_PASSWORD=test", "--env", "POSTGRES_DB=test",
        "--publish", "127.0.0.1::5432", "--label", "xuanpu.test=1",
        PG_IMAGE,
    )  # fmt: skip
    deadline = time.monotonic() + 90
    probe = ["exec", name, "pg_isready", "--host", "127.0.0.1", "--username", "postgres"]
    while _docker(*probe, check=False).returncode != 0:
        if time.monotonic() > deadline:
            _docker("rm", "--force", name, check=False)
            raise RuntimeError("Postgres 在 90s 内未就绪")
        time.sleep(0.5)
    mapping = _docker("port", name, "5432/tcp").stdout.strip().splitlines()[0]
    port = int(mapping.rsplit(":", 1)[1])
    return f"postgresql://postgres:test@127.0.0.1:{port}/test", name


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """整个测试会话共用的 Postgres。"""
    started = _start_postgres()
    if started is None:
        pytest.skip("没有可用的 Postgres：未设置 XUANPU_TEST_PG_DSN，也没有 Docker")
    dsn, container = started
    try:
        yield dsn
    finally:
        if container is not None:
            _docker("rm", "--force", "--volumes", container, check=False)


@pytest.fixture(params=["memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[ControlPlaneStore]:
    """控制面存储：同一套用例对两个实现各跑一遍。"""
    if request.param == "memory":
        yield InMemoryControlPlane()
        return
    dsn = request.getfixturevalue("postgres_dsn")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
    namespace = SqlNamespace(schema="t_" + secrets.token_hex(6), prefix="xp_")
    try:
        await apply_migrations(pool, namespace)
        yield PostgresControlPlane(pool, namespace=namespace)
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{namespace.schema}" CASCADE')
        await pool.close()


# ---------------------------------------------------------------------------
# 平台级夹具：脚本化的模型、假的 MCP server、样例业务包
# ---------------------------------------------------------------------------

FAKE_MCP_SERVER = r'''
"""测试用的最小 MCP server：两个工具，回显调用参数与注入的环境变量。"""
import json
import os
import sys

TOOLS = [
    {
        "name": "search_customer",
        "description": "按名字查客户",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
    },
    {
        "name": "update_ticket",
        "description": "更新工单",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
    },
    {
        "name": "hidden_tool",
        "description": "清单白名单之外的工具",
        "inputSchema": {"type": "object"},
    },
]


def reply(message_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if "id" not in message:
        continue
    if method == "initialize":
        reply(message["id"], {
            "protocolVersion": message["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-crm", "version": "1.0"},
        })
    elif method == "tools/list":
        reply(message["id"], {"tools": TOOLS})
    elif method == "tools/call":
        params = message["params"]
        log = os.environ.get("CALL_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(params, ensure_ascii=False) + "\n")
        text = json.dumps({
            "tool": params["name"],
            "arguments": params["arguments"],
            "region": os.environ.get("CRM_REGION"),
            "token_length": len(os.environ.get("CRM_TOKEN", "")),
            "inherited": sorted(k for k in os.environ if k.startswith("HOST_ONLY")),
        }, ensure_ascii=False)
        reply(message["id"], {"content": [{"type": "text", "text": text}]})
    else:
        reply(message["id"], {})
'''

MANIFEST = """\
apiVersion: xuanpu/v1
metadata:
  name: support-desk
  version: {version}
  description: 客服工单助手
spec:
  skills:
    entries: [triage]
  policy:
    approval: {approval}
    budget:
      maxIterations: 12
    sandbox:
      network: false
      shell: {shell}
  connectors:
    - name: crm
      command: [{python}, {server}]
      env:
        CRM_TOKEN: {{secret: crm-token}}
        CRM_REGION: {{value: cn}}
        CALL_LOG: {{value: {call_log}}}
      tools:
        - {{name: search_customer, effect: read}}
        - {{name: update_ticket, effect: write}}
"""

TRIAGE = """\
---
name: triage
description: 工单分诊
version: 1.0.0
type: composite
entry: true
child_skills: [faq]
tool_names: [mcp__crm__search_customer, mcp__crm__update_ticket, run_script{extra_tools}]
scripts:
  - name: stamp
    path: scripts/stamp.sh
    language: shell
    description: 在工作区留一个标记文件
    timeout_seconds: 20
    args_schema:
      type: object
      properties:
        label: {{type: string}}
---
# 工单分诊
先查客户信息，再决定怎么处理。
"""

FAQ = """\
---
name: faq
description: 常见问题
version: 1.0.0
type: atomic
---
# 常见问题
退款在 7 个工作日内到账。
"""


def _completed(usage: TokenUsage, *, end_turn: bool) -> ResponseEvent:
    """流的终结事件。"""
    return ResponseEvent(
        kind="completed",
        data={
            "response_id": None,
            "usage": usage.model_dump(),
            "end_turn": end_turn,
            "request_id": None,
            "stop_reason": "stop" if end_turn else "tool_calls",
        },
    )


@dataclass
class Step:
    """模型的一次采样：一段文本，外加若干工具调用。"""

    text: str = ""
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    usage: TokenUsage = field(
        default_factory=lambda: TokenUsage(input_tokens=100, output_tokens=20, total_tokens=120)
    )


class _ScriptedSession:
    """一轮采样的会话。"""

    def __init__(self, owner: ScriptedModels) -> None:
        self._owner = owner

    async def stream(self, request: Any) -> Any:
        """按脚本产出事件。"""
        self._owner.requests.append(request)
        step = self._owner.steps.pop(0) if self._owner.steps else Step(text="（脚本已用完）")
        yield ResponseEvent(kind="created")
        if step.text:
            yield ResponseEvent(kind="text_delta", data={"text": step.text})
        for call_id, name, arguments in step.calls:
            yield ResponseEvent(
                kind="tool_call_done",
                data={
                    "call_id": call_id,
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            )
        yield _completed(step.usage, end_turn=not step.calls)

    async def __aenter__(self) -> _ScriptedSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return


class ScriptedModels:
    """按脚本回复的模型：既是模型客户端，也是给平台用的客户端工厂。"""

    def __init__(self) -> None:
        self.steps: list[Step] = []
        self.requests: list[Any] = []
        self.created_for: list[tuple[str, str]] = []

    def script(self, *steps: Step) -> None:
        """追加脚本。"""
        self.steps.extend(steps)

    def session(self, *, cancel: taifeng.CancellationToken, model: str | None = None) -> Any:
        """创建一轮采样的会话。"""
        return _ScriptedSession(self)

    async def create(self, tenant_id: str, tier: str) -> taifeng.ModelClient:
        """平台为租户取模型客户端。"""
        self.created_for.append((tenant_id, tier))
        return self

    def last_prompt(self) -> str:
        """最近一次采样请求的全文。"""
        return json.dumps(self.requests[-1].model_dump(), ensure_ascii=False, default=str)


@dataclass
class World:
    """一个装配好的平台，外加测试里要用的把手。"""

    platform: Platform
    models: ScriptedModels
    data_dir: Path
    root_key: str
    call_log: Path
    package_dir: Path
    admin: Principal
    member: Principal
    admin_key: str
    member_key: str

    def archive(
        self, *, version: str = "1.0.0", approval: str = "standard", shell: bool = False
    ) -> bytes:
        """打一个样例业务包。"""
        root = self.package_dir / f"{version}-{approval}-{shell}"
        if not root.exists():
            (root / "skills" / "triage" / "scripts").mkdir(parents=True)
            (root / "skills" / "faq").mkdir(parents=True)
            server = root / "fake_mcp.py"
            server.write_text(FAKE_MCP_SERVER, encoding="utf-8")
            (root / "xuanpu.yaml").write_text(
                MANIFEST.format(
                    version=version,
                    approval=approval,
                    shell=str(shell).lower(),
                    python=json.dumps(sys.executable),
                    server=json.dumps("fake_mcp.py"),
                    call_log=json.dumps(str(self.call_log)),
                ),
                encoding="utf-8",
            )
            extra = ", run_in_background, wait_for_task" if shell else ""
            (root / "skills" / "triage" / "SKILL.md").write_text(
                TRIAGE.format(extra_tools=extra), encoding="utf-8"
            )
            (root / "skills" / "faq" / "SKILL.md").write_text(FAQ, encoding="utf-8")
            script = root / "skills" / "triage" / "scripts" / "stamp.sh"
            script.write_text('echo "stamp:$1" > "$WORKSPACE/stamp.txt"\necho "stamped $1"\n')
        return pack_package(root)

    async def publish(self, **kwargs: Any) -> None:
        """发布样例业务包并设为生效版本。"""
        await self.platform.packages.publish(self.admin, self.archive(**kwargs), activate=True)

    def calls(self) -> list[dict[str, Any]]:
        """假 MCP server 实际收到的调用。"""
        if not self.call_log.exists():
            return []
        return [json.loads(line) for line in self.call_log.read_text().splitlines()]


async def collect(turn: Any, *, timeout: float = 30.0) -> list[taifeng.EventMsg]:
    """读完一轮对话的事件。"""

    async def _read() -> list[taifeng.EventMsg]:
        return [event async for event in turn.stream.events()]

    return await asyncio.wait_for(_read(), timeout=timeout)


@pytest.fixture
def read_turn() -> Any:
    """读完一轮对话事件的函数。"""
    return collect


@pytest.fixture
def step() -> type[Step]:
    """构造模型脚本的类型。"""
    return Step


def _sandbox_provider(kind: str) -> Any:
    """按参数选沙盒供给方。Docker 变体在没有 Docker 时跳过。"""
    if kind == "local":
        return LocalSandboxProvider()
    if not _docker_available():
        pytest.skip("没有可用的 Docker 守护进程")
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    if "DOCKER_HOST" in os.environ:
        env["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
    return DockerSandboxProvider(image="python:3.12-slim", docker_env=env, memory_mb=256)


@pytest.fixture(params=["local"])
async def world(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[World]:
    """装配好的平台：内存控制面、脚本化模型、沙盒（默认本机隔离，可参数化为 Docker）；
    已有一个租户、两把凭据、一条密钥。"""
    data_dir = (tmp_path / "data").resolve()
    data_dir.mkdir()
    store = InMemoryControlPlane()
    models = ScriptedModels()
    root_key, root_hash = generate_root_key()
    platform = Platform(
        store=store,
        vault=EncryptedVault(store, keys={"k1": generate_master_key()}, primary="k1"),
        models=models,
        sandboxes=_sandbox_provider(request.param),
        data_dir=data_dir,
        root_key_hash=root_hash,
        instance_id="inst-test",
    )
    await platform.start()
    await platform.create_tenant(ROOT_PRINCIPAL, tenant_id="acme", name="Acme")
    admin = await platform.issue_key(
        ROOT_PRINCIPAL, tenant_id="acme", name="admin", role="tenant_admin"
    )
    member = await platform.issue_key(
        ROOT_PRINCIPAL, tenant_id="acme", name="app", role="tenant_member"
    )
    admin_principal = await platform.authenticate(admin.key)
    await platform.put_secret(admin_principal, "crm-token", "s3cret-token-value")
    package_dir = tmp_path / "packages-src"
    package_dir.mkdir()
    try:
        yield World(
            platform=platform,
            models=models,
            data_dir=data_dir,
            root_key=root_key,
            call_log=tmp_path / "mcp-calls.jsonl",
            package_dir=package_dir,
            admin=admin_principal,
            member=await platform.authenticate(member.key),
            admin_key=admin.key,
            member_key=member.key,
        )
    finally:
        await platform.close()


@pytest.fixture
async def serve() -> AsyncIterator[Any]:
    """返回一个函数：把一个 ASGI 应用真的跑在本机端口上，返回它的地址。用例结束时全部停掉。"""
    import socket

    import uvicorn

    running: list[tuple[uvicorn.Server, asyncio.Task[None]]] = []

    async def start(app: Any) -> str:
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
