"""连接器沙盒：stdio 连接器进程跑在每个运行时单独的沙盒里，不在平台节点上直接起。

只读业务包目录、只写自己的工作目录、可出网；读不到别的租户的数据；随运行时回收关闭；
清单里没有 stdio 连接器就不开。连接器调用本身对本机隔离与 Docker 两种后端各跑一遍。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from xuanpu_server.controlplane import AuditQuery, InMemoryControlPlane
from xuanpu_server.errors import PackageInvalidError, RuntimeUnavailableError
from xuanpu_server.runtime.connectors import ConnectorSet
from xuanpu_server.tenancy import tenant_directory
from xuanpu_spec import pack_package
from xuanpu_spec.manifest import ConnectorSpec, EnvValue

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from xuanpu_server.runtime import TenantRuntime

pytestmark = pytest.mark.skipif(
    sys.platform not in ("darwin", "linux"), reason="本机隔离后端只支持 macOS 与 Linux"
)

CREDENTIAL = "s3cret-token-value"
"""夹具给租户配好的那条密钥的明文。"""


class RecordingSandboxes:
    """包住真的沙盒供给方：记下按什么顺序开了哪些沙盒、又按什么顺序关掉。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.opened: list[str] = []
        self.closed: list[str] = []
        # 连接器沙盒开始收尾那一刻，它起过的进程各自是否还活着
        self.alive_at_close: list[bool] = []

    async def open(self, package: Any, workspace: Path) -> Any:
        """开脚本沙盒并记下。"""
        return self._track("scripts", await self.inner.open(package, workspace))

    async def open_connectors(self, package: Any, workspace: Path) -> Any:
        """开连接器沙盒并记下。"""
        return self._track("connectors", await self.inner.open_connectors(package, workspace))

    def _track(self, role: str, handle: Any) -> Any:
        """记下开了哪个沙盒，并把它的收尾换成先记一笔再照常收尾。"""
        self.opened.append(role)
        original = handle.closer

        async def closer() -> None:
            if role == "connectors":
                self.alive_at_close = [p.returncode is None for p in handle.executor.processes]
            self.closed.append(role)
            await original()

        handle.closer = closer
        return handle


@pytest.fixture
async def recorded(
    world_opener: Any, local_sandboxes: Any
) -> AsyncIterator[tuple[Any, RecordingSandboxes]]:
    """沙盒供给方被记录包住的平台（本机隔离）。"""
    sandboxes = RecordingSandboxes(local_sandboxes())
    world = await world_opener(store=InMemoryControlPlane(), sandboxes=sandboxes)
    try:
        yield world, sandboxes
    finally:
        await world.platform.close()


def only_runtime(world: Any) -> TenantRuntime:
    """平台里唯一一个已组装的运行时。"""
    runtimes = list(world.platform.runtimes._runtimes.values())  # noqa: SLF001
    assert len(runtimes) == 1
    return runtimes[0]


async def invoke_connector(world: Any, step: Any, read_turn: Any, arguments: dict[str, Any]) -> str:
    """开一个会话，让模型调一次只读的连接器工具，返回事件里的工具输出（调用须成功）。

    事件里的工具输出只是预览，太长会被截断；假 server 会原样回显参数，参数很长的用例
    改看它写在工作目录里的探测结果（``probe_report``）。
    """
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(
        step(calls=[("c1", "mcp__crm__search_customer", arguments)]), step(text="查到了")
    )
    events = await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "查")
    )
    completed = next(e for e in events if e.msg.kind == "tool_call_completed")
    assert completed.msg.data["is_error"] is False, completed.msg.data
    output: str = completed.msg.data["output"]
    return output


async def call_connector(
    world: Any, step: Any, read_turn: Any, arguments: dict[str, Any]
) -> dict[str, Any]:
    """同 ``invoke_connector``，把假 MCP server 的回包解析出来。"""
    result: dict[str, Any] = json.loads(await invoke_connector(world, step, read_turn, arguments))
    return result


def connector_workdir(world: Any, tenant_id: str = "acme") -> Path:
    """样例业务包 1.0.0 在某个租户下的连接器工作目录。"""
    root: Path = world.data_dir / "connectors" / tenant_directory(tenant_id)
    return root / "support-desk" / "1.0.0"


def probe_report(world: Any) -> dict[str, Any]:
    """假 MCP server 写在连接器工作目录里的探测结果。"""
    report: dict[str, Any] = json.loads((connector_workdir(world) / "probe.json").read_text())
    return report


async def run_containers(run_label: str) -> list[str]:
    """本次测试运行拉起、还在跑的容器：每行是「用途 版本 镜像 网络」。"""
    docker = shutil.which("docker")
    assert docker is not None
    done = await asyncio.to_thread(
        subprocess.run,  # noqa: S603 —— 参数全部来自测试代码
        [
            docker, "ps",
            "--filter", f"label={run_label}",
            "--format",
            '{{.Label "xuanpu.role"}} {{.Label "xuanpu.version"}} {{.Image}} {{.Networks}}',
        ],
        capture_output=True, text=True, timeout=60, check=True,
    )  # fmt: skip
    return sorted(done.stdout.split("\n")[:-1])


@pytest.mark.parametrize("world", ["local", "docker"], indirect=True)
async def test_connector_runs_inside_its_sandbox(
    request: pytest.FixtureRequest,
    world: Any,
    step: Any,
    read_turn: Any,
    monkeypatch: pytest.MonkeyPatch,
    docker_run_label: str,
) -> None:
    """连接器进程的 HOME 是它的工作目录；写工作目录之外被拒绝；宿主环境变量不进去。

    本机隔离与 Docker 两种后端都要满足：容器里看到的是容器内路径，但调用日志照样落在
    宿主机上这个运行时的连接器工作目录里。连接器沙盒与脚本沙盒是两个沙盒。
    """
    monkeypatch.setenv("HOST_ONLY_SECRET", "host-leak-value")
    await world.publish()
    output = await call_connector(world, step, read_turn, {"name": "张三"})

    backend = request.node.callspec.params["world"]
    runtime = only_runtime(world)
    sandbox = runtime.connector_sandbox
    assert sandbox is not None
    assert sandbox.backend == backend
    workdir = connector_workdir(world)
    assert sandbox.workspace == workdir
    if backend == "local":
        assert output["home"] == str(workdir)
    else:
        assert output["home"] == sandbox.workspace_inside == "/workspace"
        # 两个容器各带用途与版本标签；连接器容器接着网，脚本容器按清单断网
        assert await run_containers(docker_run_label) == [
            "connectors 1.0.0 python:3.12-slim bridge",
            "scripts 1.0.0 python:3.12-slim none",
        ]
    assert output["can_write_outside"] is False
    assert not (workdir.parent / "escape.txt").exists()
    assert output["inherited"] == []
    assert (output["region"], output["token_length"]) == ("cn", len(CREDENTIAL))
    assert world.calls() == [{"name": "search_customer", "arguments": {"name": "张三"}}]
    assert (workdir / "mcp-calls.jsonl").is_file()

    # 与脚本沙盒分开：执行器不同，工作目录不同
    assert sandbox.executor is not runtime.sandbox.executor
    assert runtime.sandbox.workspace.is_relative_to(world.data_dir / "workspaces")


async def test_connector_may_reach_network_while_scripts_follow_manifest(
    world: Any, step: Any, read_turn: Any
) -> None:
    """连接器沙盒允许出网（真的连得上本机一个端口）；脚本沙盒仍按清单不出网。"""
    accepted: list[bool] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(True)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        await world.publish()
        await invoke_connector(world, step, read_turn, {"name": "x", "connect": port})
    finally:
        server.close()
        await server.wait_closed()
    assert probe_report(world) == {"connect": True}
    assert accepted == [True]

    runtime = only_runtime(world)
    assert runtime.connector_sandbox is not None
    # 连接器沙盒的执行器外面包了一层记录进程的执行器
    assert runtime.connector_sandbox.executor.inner.policy.network is True  # type: ignore[attr-defined]
    assert runtime.package.manifest.spec.policy.sandbox.network is False
    assert runtime.sandbox.executor.policy.network is False  # type: ignore[attr-defined]


async def test_connector_cannot_read_other_tenants_or_script_workspace(
    world: Any, step: Any, read_turn: Any, tmp_path: Path
) -> None:
    """连接器只读得到自己的业务包与自己的工作目录：别的租户的业务包缓存、连接器工作目录、
    会话工作区，同租户的脚本工作区，以及数据目录之外的宿主文件，一概读不到。"""
    other = tenant_directory("globex")
    planted = {
        "other_package": world.data_dir / "cache" / "packages" / other / "crm" / "1.0.0" / "x.yaml",
        "other_connector": world.data_dir / "connectors" / other / "crm" / "1.0.0" / "notes.txt",
        "other_workspace": world.data_dir / "workspaces" / other / "crm" / "1.0.0" / "out.txt",
        "own_scripts": world.data_dir / "workspaces" / tenant_directory("acme") / "notes.txt",
        "host": tmp_path / "outside.txt",
    }
    for name, path in planted.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"secret of {name}")
    await world.publish()
    package = await world.platform.packages.resolve("acme", "support-desk")
    package_file = package.root / "xuanpu.yaml"
    own_file = connector_workdir(world) / "own.txt"
    own_file.parent.mkdir(parents=True, exist_ok=True)
    own_file.write_text("own notes")

    paths = [str(path) for path in planted.values()] + [str(package_file), str(own_file)]
    await invoke_connector(world, step, read_turn, {"name": "x", "read": paths})

    read = probe_report(world)["read"]
    assert {name: read[str(path)] for name, path in planted.items()} == dict.fromkeys(planted)
    assert read[str(package_file)].startswith("apiVersion")
    assert read[str(own_file)] == "own notes"


async def test_no_connector_sandbox_without_stdio_connectors(
    recorded: tuple[Any, RecordingSandboxes], step: Any, read_turn: Any
) -> None:
    """清单里只有 HTTP 连接器：不开连接器沙盒，也不建连接器工作目录。"""
    world, sandboxes = recorded
    root = world.package_dir / "http-only"
    (root / "skills" / "triage").mkdir(parents=True)
    (root / "skills" / "faq").mkdir(parents=True)
    (root / "apis").mkdir()
    (root / "xuanpu.yaml").write_text(HTTP_MANIFEST, encoding="utf-8")
    (root / "apis" / "crm.yaml").write_text(yaml.safe_dump(HTTP_OPENAPI), encoding="utf-8")
    (root / "skills" / "triage" / "SKILL.md").write_text(HTTP_TRIAGE, encoding="utf-8")
    (root / "skills" / "faq" / "SKILL.md").write_text(FAQ, encoding="utf-8")
    await world.platform.packages.publish(world.admin, pack_package(root), activate=True)

    session = await world.platform.sessions.create(world.member, package="crm-desk")
    world.models.script(step(text="您好"))
    events = await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "你好")
    )
    assert events[-1].msg.kind == "turn_completed"
    assert sandboxes.opened == ["scripts"]
    assert only_runtime(world).connector_sandbox is None
    assert not (world.data_dir / "connectors").exists()


async def test_connector_sandbox_closes_with_its_runtime(
    recorded: tuple[Any, RecordingSandboxes], step: Any, read_turn: Any
) -> None:
    """回收运行时：先关连接器进程，再关连接器沙盒，最后关脚本沙盒。

    关连接器沙盒的时候，连接器进程已经退出了（是连接器集合把它关掉的，不是靠沙盒强杀）。
    """
    world, sandboxes = recorded
    await world.publish()
    await invoke_connector(world, step, read_turn, {"name": "x"})
    runtime = only_runtime(world)
    assert sandboxes.opened == ["connectors", "scripts"]
    assert sandboxes.closed == []
    original_close = runtime.connectors.close

    async def close_connectors() -> None:
        sandboxes.closed.append("connector-processes")
        await original_close()

    runtime.connectors.close = close_connectors  # type: ignore[method-assign]

    assert await world.platform.runtimes.evict(runtime.key) is True
    assert sandboxes.closed == ["connector-processes", "connectors", "scripts"]
    assert sandboxes.alive_at_close == [False]


async def test_failed_assembly_closes_the_connector_sandbox(
    recorded: tuple[Any, RecordingSandboxes],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """连接器起不来：组装失败，已开的连接器沙盒一并收回。

    返回的提示只带异常类型；日志里有排障线索（异常、退出码、stderr 末尾），凭据按值换成
    ``***``。
    """
    world, sandboxes = recorded
    world.archive()
    root = world.package_dir / "1.0.0-standard-False"
    # 一启动就把拿到的凭据打到 stderr、再退出的连接器
    (root / "fake_mcp.py").write_text(CRASHING_SERVER)
    await world.platform.packages.publish(world.admin, pack_package(root), activate=True)
    session = await world.platform.sessions.create(world.member, package="support-desk")

    caplog.set_level(logging.DEBUG)
    with pytest.raises(RuntimeUnavailableError) as caught:
        await world.platform.sessions.send_message(world.member, session.session_id, "查")
    assert str(caught.value) == "连接器 'crm' 启动失败：RuntimeError"
    assert caught.value.__cause__ is None
    assert CREDENTIAL not in caplog.text
    [logged] = [r.getMessage() for r in caplog.records if "启动失败" in r.getMessage()]
    assert "退出码 3" in logged
    assert "token=***" in logged
    assert sandboxes.opened == ["connectors"]
    assert sandboxes.closed == ["connectors"]
    assert world.platform.runtimes._runtimes == {}  # noqa: SLF001


async def test_secret_only_reaches_the_connector_process(
    world: Any, step: Any, read_turn: Any
) -> None:
    """密钥只进连接器进程的环境：连接器拿得到；脚本沙盒的环境与整份审计里都没有。"""
    await world.publish()
    output = await call_connector(world, step, read_turn, {"name": "x"})
    assert output["token_length"] == len(CREDENTIAL)

    runtime = only_runtime(world)
    assert "CRM_TOKEN" not in runtime.sandbox.process_env()
    records = await world.platform.store.query_audit("acme", AuditQuery(limit=1000))
    assert any(record.kind == "connector_call" for record in records)
    assert CREDENTIAL not in json.dumps([record.data for record in records], ensure_ascii=False)


async def test_stdio_connector_without_a_sandbox_is_refused(tmp_path: Path) -> None:
    """没有连接器沙盒（没有执行器）时遇到 stdio 连接器直接报错，不在平台节点上起进程。"""
    marker = tmp_path / "started.txt"
    spec = ConnectorSpec.model_validate(
        {
            "name": "crm",
            "command": [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"],
        }
    )
    # 走到「有没有执行器」这一步之前用不到保管库、审计与对照表
    connectors = ConnectorSet(
        tenant_id="acme", vault=None, audit=None, submissions=None, executor=None
    )
    with pytest.raises(RuntimeUnavailableError, match="没有连接器沙盒"):
        await connectors.start(spec, package_root=tmp_path)
    assert not marker.exists()


HTTP_MANIFEST = """\
apiVersion: xuanpu/v1
metadata: {name: crm-desk, version: 1.0.0}
spec:
  skills: {entries: [triage]}
  policy: {approval: standard}
  connectors:
    - name: crm
      transport: http
      openapi: apis/crm.yaml
      baseUrl: http://127.0.0.1:9/api
      timeoutSeconds: 5
"""

HTTP_OPENAPI: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "CRM"},
    "paths": {"/customers": {"get": {"operationId": "listCustomers", "summary": "列客户"}}},
}

HTTP_TRIAGE = """\
---
name: triage
description: 工单分诊
version: 1.0.0
type: composite
entry: true
child_skills: [faq]
tool_names: [api__crm__listCustomers]
---
# 工单分诊
"""

FAQ = "---\nname: faq\ndescription: 常见问题\nversion: 1.0.0\ntype: atomic\n---\n正文\n"


class RefusingExecutor:
    """记下有没有人让它起过进程；真被调用就报错。"""

    def __init__(self) -> None:
        self.started: list[Any] = []

    async def start(self, spec: Any) -> Any:
        """不该走到这里。"""
        self.started.append(spec)
        raise AssertionError("不该启动连接器进程")


@pytest.mark.parametrize("name", ["LD_PRELOAD", "ld_library_path", "DYLD_INSERT_LIBRARIES"])
async def test_loader_variables_never_reach_the_launcher(tmp_path: Path, name: str) -> None:
    """清单里的动态链接器变量（绕过规范校验构造的也一样）在起进程之前就被拒绝。

    本机沙盒的启动器在沙盒之外拿到连接器的整份环境，这类变量能让它在沙盒外执行代码。
    """
    spec = ConnectorSpec.model_construct(
        name="crm",
        transport="stdio",
        command=("python3", "server.py"),
        env={name: EnvValue(value="/tmp/evil.so"), "CRM_REGION": EnvValue(value="cn")},
        tools=(),
    )
    executor = RefusingExecutor()
    connectors = ConnectorSet(
        tenant_id="acme", vault=None, audit=None, submissions=None, executor=executor
    )
    with pytest.raises(PackageInvalidError, match="动态链接器") as caught:
        await connectors.start(spec, package_root=tmp_path)
    assert name in str(caught.value)
    assert executor.started == []


CRASHING_SERVER = """\
import os, sys, time
sys.stderr.write("token=" + os.environ["CRM_TOKEN"] + "\\n")
sys.stderr.flush()
time.sleep(0.3)
sys.exit(3)
"""

TOOLS_LIST_FAILS = """\
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {
            "protocolVersion": message["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "broken", "version": "1"},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    else:
        error = {"code": -32603, "message": "列不出工具"}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": error}), flush=True)
"""

HANGING_SERVER = """\
import os, time
with open(os.path.join(os.environ["HOME"], "pid.txt"), "w") as handle:
    handle.write(str(os.getpid()))
time.sleep(600)
"""


def group_alive(pgid: int) -> bool:
    """进程组里是否还有进程。"""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def test_startup_timeout_does_not_leak_the_connector_process(
    tmp_path: Path, local_sandboxes: Any
) -> None:
    """连接器启动卡住、超过启动时限：内核不收回已经起来的进程，关连接器沙盒时把它强杀。

    连接器集合与连接器沙盒依次关闭之后，进程（连同它的进程组）确实没了。
    """
    package_root = tmp_path / "pkg"
    package_root.mkdir()
    (package_root / "hang.py").write_text(HANGING_SERVER)
    handle = await local_sandboxes().open_connectors(
        SimpleNamespace(root=package_root), tmp_path / "work"
    )
    spec = ConnectorSpec.model_validate({"name": "crm", "command": [sys.executable, "hang.py"]})
    connectors = ConnectorSet(
        tenant_id="acme",
        vault=None,
        audit=None,
        submissions=None,
        executor=handle.executor,
        base_env=handle.process_env(),
        startup_timeout_seconds=1.0,
    )
    with pytest.raises(RuntimeUnavailableError, match="启动失败"):
        await connectors.start(spec, package_root=package_root)
    assert (tmp_path / "work" / "pid.txt").is_file()
    [process] = handle.executor.processes
    # 内核没有收回它：这正是要兜底的情形
    assert process.returncode is None

    await connectors.close()
    await handle.close()
    assert process.returncode is not None
    deadline = time.monotonic() + 5
    while group_alive(process.pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not group_alive(process.pid)
    assert handle.executor.processes == ()
    with pytest.raises(OSError, match="已关闭"):
        await handle.executor.start(None)


async def open_bare_connector(
    tmp_path: Path, local_sandboxes: Any, script: str, *, startup_timeout: float = 30.0
) -> tuple[Any, ConnectorSet, ConnectorSpec, Path]:
    """不经平台：开一个本机连接器沙盒，备好跑 ``script`` 的连接器。"""
    package_root = tmp_path / "pkg"
    package_root.mkdir()
    (package_root / "server.py").write_text(script)
    handle = await local_sandboxes().open_connectors(
        SimpleNamespace(root=package_root), tmp_path / "work"
    )
    spec = ConnectorSpec.model_validate({"name": "crm", "command": [sys.executable, "server.py"]})
    connectors = ConnectorSet(
        tenant_id="acme",
        vault=None,
        audit=None,
        submissions=None,
        executor=handle.executor,
        base_env=handle.process_env(),
        startup_timeout_seconds=startup_timeout,
    )
    return handle, connectors, spec, package_root


async def test_failing_tools_list_is_a_startup_failure(
    tmp_path: Path, local_sandboxes: Any
) -> None:
    """握手成功、列工具失败：同样算启动失败（只带异常类型），关闭后进程退出。"""
    handle, connectors, spec, root = await open_bare_connector(
        tmp_path, local_sandboxes, TOOLS_LIST_FAILS
    )
    with pytest.raises(RuntimeUnavailableError) as caught:
        await connectors.start(spec, package_root=root)
    assert str(caught.value) == "连接器 'crm' 启动失败：McpToolError"
    [process] = handle.executor.processes
    await connectors.close()
    assert process.returncode is not None
    await handle.close()


async def call_failing(
    world: Any, step: Any, read_turn: Any, arguments: dict[str, Any]
) -> dict[str, Any]:
    """让模型调一次连接器工具（预期失败），返回那次调用的结果事件数据。"""
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(
        step(calls=[("c1", "mcp__crm__search_customer", arguments)]), step(text="好")
    )
    events = await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "查")
    )
    data: dict[str, Any] = next(e.msg.data for e in events if e.msg.kind == "tool_call_completed")
    assert data["is_error"] is True
    return data


async def gate_details(world: Any) -> list[str]:
    """过闸记录里的 detail。"""
    records = await world.platform.store.query_audit("acme", AuditQuery(limit=1000))
    return [record.data["detail"] for record in records if record.kind == "connector_call"]


async def test_tool_error_text_is_redacted_everywhere(
    world: Any, step: Any, read_turn: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """工具回了消息里带凭据的错误：交给模型的文本、审计的 detail、日志里都换成了 ***。"""
    caplog.set_level(logging.DEBUG)
    await world.publish()
    data = await call_failing(world, step, read_turn, {"name": "x", "fail": True})
    assert "后端拒绝了 token=***" in data["output"]
    assert CREDENTIAL not in data["output"]
    assert await gate_details(world) == ["[-32001] 后端拒绝了 token=***"]
    assert "调用失败" in caplog.text
    assert CREDENTIAL not in caplog.text


async def test_connector_dying_mid_call_logs_exit_code_and_last_words(
    world: Any, step: Any, read_turn: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """调用途中连接器进程退出：调用记为失败并进审计；日志里有退出码与脱敏后的遗言。"""
    caplog.set_level(logging.DEBUG)
    await world.publish()
    data = await call_failing(world, step, read_turn, {"name": "x", "exit": 7})
    assert "connector_error" in data["output"]
    assert len(await gate_details(world)) == 1
    [logged] = [r.getMessage() for r in caplog.records if "调用失败" in r.getMessage()]
    assert "退出码 7" in logged
    assert "dying with token=***" in logged
    assert CREDENTIAL not in caplog.text


async def test_noise_on_connector_stdout_stays_out_of_the_log(
    world: Any, step: Any, read_turn: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """连接器往 stdout 写了带凭据的非 JSON 行：内核照常跳过它，日志里只记长度不记内容。"""
    caplog.set_level(logging.DEBUG)
    await world.publish()
    output = await call_connector(world, step, read_turn, {"name": "x", "noise": True})
    assert output["token_length"] == len(CREDENTIAL)
    assert "内容不记" in caplog.text
    assert CREDENTIAL not in caplog.text
