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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncpg
import pytest
from taifeng import ResponseEvent, TokenUsage
from taifeng_stores import SqlNamespace
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.controlplane.postgres import PostgresControlPlane
from xuanpu_server.controlplane.schema import apply_migrations
from xuanpu_server.identity import InMemoryIdentityStore
from xuanpu_server.identity.config import IdentityConfig
from xuanpu_server.identity.postgres import PostgresIdentityStore
from xuanpu_server.identity.service import IdentitySettings
from xuanpu_server.objectstore import LocalObjectStore
from xuanpu_server.platform import ROOT_PRINCIPAL, Platform
from xuanpu_server.runtime import DockerSandboxProvider, LocalSandboxProvider
from xuanpu_server.secrets import EncryptedVault, generate_master_key
from xuanpu_server.tenancy import generate_root_key
from xuanpu_spec import archive_digest, pack_package

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    import taifeng
    from xuanpu_server.controlplane import ControlPlaneStore
    from xuanpu_server.identity import IdentityStore
    from xuanpu_server.runtime import SandboxProvider, StateBackendFactory
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


@asynccontextmanager
async def _migrated_schema(dsn: str) -> AsyncIterator[tuple[asyncpg.Pool, SqlNamespace]]:
    """一个独占的、已迁移到最新的 schema；用完连同里面的表一起删掉。"""
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
    namespace = SqlNamespace(schema="t_" + secrets.token_hex(6), prefix="xp_")
    try:
        await apply_migrations(pool, namespace)
        yield pool, namespace
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{namespace.schema}" CASCADE')
        await pool.close()


@pytest.fixture(params=["memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[ControlPlaneStore]:
    """控制面存储：同一套用例对两个实现各跑一遍。"""
    if request.param == "memory":
        yield InMemoryControlPlane()
        return
    async with _migrated_schema(request.getfixturevalue("postgres_dsn")) as (pool, namespace):
        yield PostgresControlPlane(pool, namespace=namespace)


@pytest.fixture(params=["memory", "postgres"])
async def identity_stores(
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[ControlPlaneStore, IdentityStore]]:
    """同一后端上的控制面存储与身份存储：身份域的契约用例对两个实现各跑一遍。

    身份存储以控制面为准判断租户是否存在（Postgres 靠外键，内存实现构造时拿到控制面），所以
    两者共用同一后端，用例先经控制面开通租户。
    """
    if request.param == "memory":
        control = InMemoryControlPlane()
        yield control, InMemoryIdentityStore(control)
        return
    async with _migrated_schema(request.getfixturevalue("postgres_dsn")) as (pool, namespace):
        yield (
            PostgresControlPlane(pool, namespace=namespace),
            PostgresIdentityStore(pool, namespace=namespace),
        )


# ---------------------------------------------------------------------------
# 平台级夹具：脚本化的模型、假的 MCP server、样例业务包
# ---------------------------------------------------------------------------

DATA = Path(__file__).parent / "data"
"""测试数据：假的 MCP server（两个工具，回显参数与注入的环境变量）、样例业务包的模板。"""
MANIFEST = (DATA / "support-desk.xuanpu.yaml.tmpl").read_text(encoding="utf-8")
TRIAGE = (DATA / "triage.SKILL.md.tmpl").read_text(encoding="utf-8")


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
    """模型的一次采样：一段文本，外加若干工具调用。

    ``wait`` 只为测试「停止生成」：给了就在产出文本之后、结束之前等它被置位，让这次采样
    卡在半路；取消时等待处收到 ``CancelledError``，照常上抛。
    """

    text: str = ""
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    usage: TokenUsage = field(
        default_factory=lambda: TokenUsage(input_tokens=100, output_tokens=20, total_tokens=120)
    )
    wait: asyncio.Event | None = None


class _ScriptedSession:
    """一轮采样的会话。"""

    def __init__(self, owner: ScriptedModels) -> None:
        self._owner = owner

    async def stream(self, request: Any) -> Any:
        """按脚本产出事件。"""
        self._owner.requests.append(request)
        step = self._owner.next_step(request)
        yield ResponseEvent(kind="created")
        if step.text:
            yield ResponseEvent(kind="text_delta", data={"text": step.text})
        if step.wait is not None:
            # 卡在半路，直到测试放行或这一轮被取消（CancelledError 不在这里吞掉）
            await step.wait.wait()
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


def _last_user_input(request: Any) -> str | None:
    """采样请求里最后一条用户输入的文本；没有用户输入时为 None。"""
    for message in reversed(request.messages or []):
        if message.role == "user":
            content = message.content
            return content if isinstance(content, str) else json.dumps(content, default=str)
    return None


class ScriptedModels:
    """按脚本回复的模型：既是模型客户端，也是给平台用的客户端工厂。"""

    def __init__(self) -> None:
        self.steps: list[Step] = []
        self.keyed: dict[str, list[Step]] = {}
        self.requests: list[Any] = []
        self.created_for: list[tuple[str, str]] = []

    def script(self, *steps: Step) -> None:
        """追加脚本：按采样的先后顺序取用。"""
        self.steps.extend(steps)

    def script_for(self, fragment: str, *steps: Step) -> None:
        """追加只给某类采样用的脚本：最后一条用户输入里含 ``fragment`` 的采样先取这里的。

        几个会话并发跑、谁先采样说不准时（例如评测用例并发执行），用它按输入对上回复。
        只看最后一条用户输入，不看整个请求：片段可能恰好出现在 skill 文本或历史里。
        """
        self.keyed.setdefault(fragment, []).extend(steps)

    def next_step(self, request: Any) -> Step:
        """取这次采样要用的一步：先找按最后一条用户输入对上的脚本，没有再按顺序取。"""
        latest = _last_user_input(request)
        for fragment, queue in self.keyed.items():
            if queue and latest is not None and fragment in latest:
                return queue.pop(0)
        return self.steps.pop(0) if self.steps else Step(text="（脚本已用完）")

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
    objects: LocalObjectStore
    objects_dir: Path
    root_key: str
    package_dir: Path
    admin: Principal
    member: Principal
    admin_key: str
    member_key: str
    states: StateBackendFactory | None = None
    python: str = sys.executable
    """样例业务包里连接器命令用的解释器：本机沙盒用测试进程的解释器，容器沙盒用镜像里的。"""

    def sibling(self, instance_id: str, *, data_dir: Path, **kwargs: Any) -> Platform:
        """同一份控制面、密钥、模型、对象存储与状态面上的另一个平台实例（未启动）。

        ``data_dir`` 必须显式给出：数据目录归一个进程独占，几个实例共用它只在 local
        状态面下为了跨实例接手会话才这么做（会话主存在数据目录里）。
        """
        return Platform(
            store=self.platform.store,
            identity_store=self.platform.identity.store,
            identity_settings=self.platform.identity.settings,
            vault=self.platform.vault,
            models=self.models,
            sandboxes=local_sandboxes(),
            data_dir=data_dir,
            root_key_hash="0" * 64,
            objects=self.objects,
            states=self.states,
            instance_id=instance_id,
            **kwargs,
        )

    def archive(
        self, *, version: str = "1.0.0", approval: str = "standard", shell: bool = False
    ) -> bytes:
        """打一个样例业务包。"""
        root = self.package_dir / f"{version}-{approval}-{shell}"
        if not root.exists():
            (root / "skills" / "triage" / "scripts").mkdir(parents=True)
            (root / "skills" / "faq").mkdir(parents=True)
            shutil.copyfile(DATA / "fake_mcp_server.py", root / "fake_mcp.py")
            (root / "xuanpu.yaml").write_text(
                MANIFEST.format(
                    version=version,
                    approval=approval,
                    shell=str(shell).lower(),
                    python=json.dumps(self.python),
                    server=json.dumps("fake_mcp.py"),
                ),
                encoding="utf-8",
            )
            extra = ", run_in_background, wait_for_task" if shell else ""
            (root / "skills" / "triage" / "SKILL.md").write_text(
                TRIAGE.format(extra_tools=extra), encoding="utf-8"
            )
            shutil.copyfile(DATA / "faq.SKILL.md", root / "skills" / "faq" / "SKILL.md")
            script = root / "skills" / "triage" / "scripts" / "stamp.sh"
            script.write_text('echo "stamp:$1" > "$WORKSPACE/stamp.txt"\necho "stamped $1"\n')
        return pack_package(root)

    async def publish(self, **kwargs: Any) -> None:
        """发布样例业务包并设为生效版本。"""
        await self.platform.packages.publish(self.admin, self.archive(**kwargs), activate=True)

    def archive_key(self, **kwargs: Any) -> str:
        """样例业务包（参数同 ``archive``）在对象存储里应有的键：版本目录下按指纹命名。"""
        version = kwargs.get("version", "1.0.0")
        hexdigest = archive_digest(self.archive(**kwargs)).removeprefix("sha256:")
        return f"tenants/acme/packages/support-desk/{version}/{hexdigest}.zip"

    def object_keys(self) -> list[str]:
        """对象存储里现有的全部键，已排序。"""
        if not self.objects_dir.exists():
            return []
        return sorted(
            path.relative_to(self.objects_dir).as_posix()
            for path in self.objects_dir.rglob("*")
            if path.is_file()
        )

    def calls(self) -> list[dict[str, Any]]:
        """假 MCP server 实际收到的调用。

        假 server 把调用日志写在自己的 ``$HOME``，也就是某个实例数据目录下的连接器工作目录
        （``<data_dir>/connectors/<租户>/<名>/<版本>/``）。同一用例里可能有几个实例（各有
        数据目录，都在 ``data_dir`` 的上一级），所以在所有实例的连接器目录里找；调用日志
        应当只有一份，多了说明用例里有几个运行时都调用过，直接报错而不是随便挑一份。
        """
        logs = [
            path
            for connectors in sorted(self.data_dir.parent.glob("*/connectors"))
            for path in connectors.rglob("mcp-calls.jsonl")
        ]
        if not logs:
            return []
        assert len(logs) == 1, f"调用日志不止一份：{logs}"
        return [json.loads(line) for line in logs[0].read_text().splitlines()]


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


CONNECTOR_READABLE_ROOTS = tuple(
    dict.fromkeys(Path(prefix).resolve() for prefix in (sys.prefix, sys.base_prefix))
)
"""本机连接器沙盒里额外可读的目录：假 MCP server 用测试进程的解释器跑，要能读到虚拟环境
（``sys.prefix``）与它背后的解释器安装目录（``sys.base_prefix``）。"""


def local_sandboxes() -> LocalSandboxProvider:
    """本机隔离的沙盒供给方，连接器沙盒能读到运行假 MCP server 的解释器。"""
    return LocalSandboxProvider(connector_readable_roots=CONNECTOR_READABLE_ROOTS)


@pytest.fixture(name="local_sandboxes")
def local_sandboxes_fixture() -> Any:
    """返回造本机沙盒供给方的函数，给自己装配平台的用例用。"""
    return local_sandboxes


DOCKER_RUN_LABEL = ("xuanpu.test-run", secrets.token_hex(6))
"""本次测试运行拉起的容器都带这个标签：按它找容器，不会碰到别处（或别的测试运行）的容器。"""


@pytest.fixture
def docker_run_label() -> str:
    """``docker ps --filter label=…`` 用的本次测试运行的标签。"""
    return "=".join(DOCKER_RUN_LABEL)


def docker_test_user() -> str | None:
    """rootful Linux测试匹配私有绑定目录的所有者；Mac共享保留镜像默认身份。"""
    return f"{os.getuid()}:{os.getgid()}" if sys.platform == "linux" else None


def _sandbox_provider(kind: str) -> Any:
    """按参数选沙盒供给方。Docker 变体在没有 Docker 时跳过。"""
    if kind == "local":
        return local_sandboxes()
    if not _docker_available():
        pytest.skip("没有可用的 Docker 守护进程")
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    if "DOCKER_HOST" in os.environ:
        env["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
    return DockerSandboxProvider(
        user=docker_test_user(),
        image="python:3.12-slim",
        docker_env=env,
        memory_mb=256,
        labels=dict([DOCKER_RUN_LABEL]),
    )


async def open_world(
    tmp_path: Path,
    *,
    store: ControlPlaneStore,
    sandboxes: SandboxProvider,
    states: StateBackendFactory | None = None,
    instance_id: str = "inst-test",
    data_dir_name: str = "data",
    metrics_token_hash: str | None = None,
    identity_store: IdentityStore | None = None,
    identity_settings: IdentitySettings | None = None,
    durable_execution: Any = None,
    connections: Any = None,
) -> World:
    """在 ``tmp_path`` 下装配并启动一个平台，开通一个租户、两把凭据、一条密钥。

    ``metrics_token_hash`` 给了就开指标端点（抓取令牌的摘要）。``identity_store`` 必须与
    ``store`` 同一后端：内存控制面可以省略（用以它为准的内存身份存储），其余必须显式给出。
    ``identity_settings`` 省略时按默认配置构造（没有 ``public_url``，平台不认登录 cookie）。

    对象存储放在数据目录之外（``tmp_path/objects``），几个实例可以共用。返回之后平台由
    调用方关闭；开通到一半失败时这里先关掉它。
    """
    data_dir = (tmp_path / data_dir_name).resolve()
    data_dir.mkdir()
    objects_dir = (tmp_path / "objects").resolve()
    objects = LocalObjectStore(objects_dir)
    models = ScriptedModels()
    root_key, root_hash = generate_root_key()
    if identity_store is None:
        if not isinstance(store, InMemoryControlPlane):
            raise TypeError("非内存的控制面要显式给出同一后端的 identity_store")
        identity_store = InMemoryIdentityStore(store)
    if identity_settings is None:
        identity_settings = IdentitySettings.from_config(None, IdentityConfig())
    platform = Platform(
        store=store,
        identity_store=identity_store,
        identity_settings=identity_settings,
        vault=EncryptedVault(store, keys={"k1": generate_master_key()}, primary="k1"),
        models=models,
        sandboxes=sandboxes,
        data_dir=data_dir,
        root_key_hash=root_hash,
        instance_id=instance_id,
        objects=objects,
        states=states,
        metrics_token_hash=metrics_token_hash,
        durable_execution=durable_execution,
        connections=connections,
    )
    await platform.start()
    try:
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
        return World(
            platform=platform,
            models=models,
            data_dir=data_dir,
            objects=objects,
            objects_dir=objects_dir,
            root_key=root_key,
            package_dir=package_dir,
            admin=admin_principal,
            member=await platform.authenticate(member.key),
            admin_key=admin.key,
            member_key=member.key,
            states=states,
            # 容器里没有宿主的解释器：连接器命令改用镜像里的 python3
            python="python3" if isinstance(sandboxes, DockerSandboxProvider) else sys.executable,
        )
    except BaseException:
        # 开通到一半失败：平台已经启动（有后台续期任务），关掉再上抛
        await platform.close()
        raise


@pytest.fixture
def world_opener(tmp_path: Path) -> Any:
    """返回 ``open_world`` 绑定好本用例临时目录的版本，给需要换后端的用例自己装配平台。"""

    async def opener(**kwargs: Any) -> World:
        """装配一个平台，参数同 ``open_world``（临时目录已绑定）。"""
        return await open_world(tmp_path, **kwargs)

    return opener


@pytest.fixture(params=["local"])
async def world(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[World]:
    """装配好的平台：内存控制面、脚本化模型、沙盒（默认本机隔离，可参数化为 Docker）、
    数据目录之外的本机对象存储、local 状态面；已有一个租户、两把凭据、一条密钥。"""
    world = await open_world(
        tmp_path, store=InMemoryControlPlane(), sandboxes=_sandbox_provider(request.param)
    )
    try:
        yield world
    finally:
        await world.platform.close()


@pytest.fixture
def object_gets(world: World, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """记下 ``world`` 的对象存储每次 ``get`` 取的键（按调用先后），用来数下载次数。

    每次读取前让出一次事件循环，给并发的读取留出交错的机会。发布不读对象存储，所以从
    用例一开始就计数也不影响结果。
    """
    seen: list[str] = []
    original = world.objects.get

    async def counting(key: str) -> bytes:
        """记下键、让出一次事件循环，再照常读取。"""
        seen.append(key)
        await asyncio.sleep(0)
        return await original(key)

    monkeypatch.setattr(world.objects, "get", counting)
    return seen
