"""工作任务复用真实员工、双后端事务和对象存储。"""

from __future__ import annotations

import hashlib
import io
import time
import zipfile
from types import SimpleNamespace

import pytest
from xuanpu_server.identity.models import UserRecord
from xuanpu_server.objectstore import LocalObjectStore
from xuanpu_server.tenancy import Principal, Tenant
from xuanpu_server.work_tasks.models import InputSnapshot, WorkTaskRecord
from xuanpu_server.workspace.service import WorkspaceService


@pytest.fixture
async def task_env(identity_stores, tmp_path):
    """测试均使用实际存储，不替换数据库事务或文件权限。"""
    control, identity = identity_stores
    await control.create_tenant(Tenant(tenant_id="acme", name="公司", created_at=1.0))
    for user in ("usr_a", "usr_b"):
        await identity.create_user(UserRecord(tenant_id="acme", user_id=user,
                                             display_name=user, created_at=1.0))
    workspace = WorkspaceService(store=control.workspace_store, control=control, identity=identity,
        objects=LocalObjectStore(tmp_path / "objects"), now=lambda: 1000.0)
    who = Principal(role="tenant_member", tenant_id="acme", user_id="usr_a", key_id=None)
    file = await workspace.upload(who, "meeting.txt", b"private minutes", "text/plain", "file")
    snapshot = InputSnapshot(item_id=file["id"], revision=1, sha256=hashlib.sha256(b"private minutes").hexdigest(),
        size=15, mime_type="text/plain", relative_name="input_1.txt")
    record = WorkTaskRecord(task_id="task1", tenant_id="acme", user_id="usr_a", actor=who.actor,
        request_id="req1", input_fingerprint="a" * 64, text="整理会议PRIVATE", timezone="Asia/Shanghai",
        inputs=(snapshot,), package="office-assistant", package_version="1.0.0", package_digest="b" * 64,
        process_owner="p1", accepted_at=1000.0, updated_at=1000.0)
    return SimpleNamespace(workspace=workspace, who=who, identity=identity, control=control,
                           record=record, file=file)


@pytest.fixture
async def task_api(world_opener, local_sandboxes):
    """使用实际cookie认证与已发布的测试办公包，不开启外部通知。"""
    import httpx
    from xuanpu_server.api import create_app
    from xuanpu_server.controlplane import InMemoryControlPlane
    from xuanpu_server.identity import LoginSession
    from xuanpu_server.identity.config import IdentityConfig
    from xuanpu_server.identity.cookies import login_cookie_name, new_login_token
    from xuanpu_server.identity.service import IdentitySettings

    settings = IdentitySettings.from_config("https://xuanpu.example.com", IdentityConfig())
    world = await world_opener(store=InMemoryControlPlane(), sandboxes=local_sandboxes(),
                               identity_settings=settings)
    await world.platform.task_registry.close()
    world.platform.work_tasks.executor = None
    archive = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(world.archive())) as original, zipfile.ZipFile(archive, "w") as output:
        for name in original.namelist():
            content = original.read(name)
            if name == "xuanpu.yaml":
                content = content.replace(b"support-desk", b"office-assistant")
            output.writestr(name, content)
    await world.platform.packages.publish(world.admin, archive.getvalue(), activate=True)
    users = {}
    for user_id in ("usr_a", "usr_b"):
        await world.platform.identity.store.create_user(UserRecord(tenant_id="acme", user_id=user_id,
            display_name=user_id, created_at=time.time()))
        token, hashed = new_login_token()
        await world.platform.identity.store.create_login_session(LoginSession(token_hash=hashed,
            tenant_id="acme", user_id=user_id, created_at=time.time(), expires_at=time.time() + 3600,
            last_seen_at=time.time()))
        users[user_id] = {"Cookie": f"{login_cookie_name(settings.cookie_secure)}={token}",
            "Origin": "https://xuanpu.example.com", "X-Requested-By": "xuanpu"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(world.platform)),
                                 base_url="https://xuanpu.example.com") as client:
        try:
            yield SimpleNamespace(world=world, client=client, users=users)
        finally:
            await world.platform.close()


@pytest.fixture(scope='session')
def office_image(tmp_path_factory):
    """独立办公镜像只从两个白名单构建文件创建，不发送仓库或运行数据。"""
    import shutil
    import subprocess
    from pathlib import Path

    repository = Path(__file__).resolve().parents[4]
    tag = 'xuanpu-office:r1-20261021'
    fingerprint = hashlib.sha256((repository / 'docker/office-requirements.lock').read_bytes()
        + (repository / 'docker/office-sandbox.Dockerfile').read_bytes()).hexdigest()
    inspected = subprocess.run(['docker', 'image', 'inspect', '--format',
        '{{index .Config.Labels "xuanpu.office.build"}}', tag], capture_output=True, text=True)
    if inspected.returncode == 0 and inspected.stdout.strip() == fingerprint:
        return tag
    context = tmp_path_factory.mktemp('office-build')
    shutil.copy(repository / 'docker/office-sandbox.Dockerfile', context / 'Dockerfile')
    shutil.copy(repository / 'docker/office-requirements.lock', context / 'office-requirements.lock')
    result = subprocess.run(['docker','build','--label', 'xuanpu.office.build=' + fingerprint,
        '-t',tag,str(context)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return tag


@pytest.fixture
async def office_worker(office_image, tmp_path):
    """通过真实沙盒守护进程调用业务脚本，执行时断网并只读挂载输入和包。"""
    import shlex
    from pathlib import Path

    import taifeng
    from taifeng_sandbox.docker import DockerEnvironment, DockerSandboxConfig, Mount

    package = Path(__file__).resolve().parents[4] / 'examples/office-assistant'
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    workspace = tmp_path / 'work'
    workspace.mkdir()
    (workspace / 'inputs').mkdir()
    config = DockerSandboxConfig(image=office_image, workspace_host_dir=workspace, memory_mb=512,
        cpus=1, network=False, docker_env={'PATH': '/usr/local/bin:/usr/bin:/bin',
                                         'HOME':str(Path.home())},
        mounts=(Mount(package, '/package'), Mount(inputs, '/workspace/inputs')),
        labels={'xuanpu.test':'1', 'xuanpu.office':'1'})
    environment = await DockerEnvironment.create(config)

    async def run(script, contents, args):
        """每次输入由宿主固定，脚本不能替换输入或访问宿主文件。"""
        for name, data in contents.items():
            (inputs / name).write_bytes(data)
        process = await environment.executor().start(taifeng.CommandSpec(
            command=shlex.join(['python3','-u','-I','/package/skills/' + script,*args]), shell=False,
            cwd='/workspace', env={'PATH':'/usr/local/bin:/usr/bin:/bin', 'WORKSPACE':'/workspace'}))
        stdout, stderr = await process.communicate()
        code = await process.wait()
        return SimpleNamespace(code=code, stdout=stdout.decode(), stderr=stderr.decode(),
                               workspace=workspace, filesystem=environment.workspace())

    try:
        yield run
    finally:
        await environment.close()


@pytest.fixture
async def office_world(identity_stores, world_opener, office_image):
    """真实业务包、员工、双后端事务和独立办公镜像组成完整平台。"""
    from pathlib import Path

    from xuanpu_server.runtime.sandbox import DockerSandboxProvider
    from xuanpu_spec import pack_package

    control, identity = identity_stores
    provider = DockerSandboxProvider(image=office_image,
        docker_env={'PATH':'/usr/local/bin:/usr/bin:/bin', 'HOME':str(Path.home())},
        memory_mb=512, labels={'xuanpu.test':'1'})
    world = await world_opener(store=control, identity_store=identity, sandboxes=provider)
    await world.platform.packages.publish(world.admin,
        pack_package(Path(__file__).resolve().parents[4] / 'examples/office-assistant'), activate=True)
    await identity.create_user(UserRecord(tenant_id='acme', user_id='usr_office',
        display_name='办公员工', created_at=time.time()))
    world.office_user = Principal(role='tenant_member', tenant_id='acme', user_id='usr_office', key_id=None)
    return world
