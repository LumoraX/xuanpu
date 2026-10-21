"""运行时回收失败保留所有权，不能创建第二个同scope实例。"""

from __future__ import annotations

import pytest
from xuanpu_server.errors import RuntimeUnavailableError
from xuanpu_server.runtime.session_requests import scope_for


async def test_failed_eviction_keeps_runtime_and_blocks_reuse(world, step, read_turn, monkeypatch):
    """清理失败后同scope不能重新起一组进程，后续关闭仍有资源引用。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="done"))
    await read_turn(await world.platform.sessions.send_message(world.member, session.session_id, "hi"))
    await world.platform.sessions.close(world.member, session.session_id)
    runtime = next(iter(world.platform.runtimes._runtimes.values()))
    runtime.last_used = 0

    async def fail():
        """模拟进程无法退出，关闭必须失败。"""
        raise RuntimeError("cannot reap")

    with monkeypatch.context() as patch:
        patch.setattr(runtime, "close", fail)
        with pytest.raises(RuntimeError, match="cannot reap"):
            await world.platform.runtimes.evict_idle()
        assert world.platform.runtimes._runtimes[runtime.key] is runtime
        with pytest.raises(RuntimeUnavailableError):
            await world.platform.runtimes.get(runtime.package, scope_for(session))


async def test_failed_assembly_retains_cleanup_and_blocks_duplicate_scope(world, monkeypatch):
    """装配失败后，清理错误不能丢资源，平台关闭仍须看到未清理项。"""
    await world.publish()
    runtimes = world.platform.runtimes
    package = await world.platform.packages.resolve("acme", "support-desk")
    original_open = runtimes._sandboxes.open
    opened = []

    async def fail_close():
        """模拟进程或容器无法回收。"""
        raise RuntimeError("cannot reap resource")

    async def open_sandbox(*args, **kwargs):
        """保留真实沙盒句柄，并注入关闭故障。"""
        handle = await original_open(*args, **kwargs)
        opened.append((handle, handle.closer))
        handle.closer = fail_close
        return handle

    async def fail_pool(*args, **kwargs):
        """在已创建沙盒之后模拟建池失败。"""
        raise RuntimeError("pool assembly failed")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(runtimes._sandboxes, "open", open_sandbox)
            patch.setattr(runtimes, "_create_pool", fail_pool)
            with pytest.raises(Exception):
                await runtimes.get(package)
            with pytest.raises(RuntimeUnavailableError):
                await runtimes.get(package)
            assert len(opened) == 1
            with pytest.raises(ExceptionGroup):
                await runtimes.close()
    finally:
        for handle, closer in opened:
            handle.closer = closer
            await handle.close()


async def test_failed_container_start_keeps_known_cleanup_owner(world_opener, monkeypatch):
    """容器已创建但daemon未连通时，宿主仍知道应回收哪一个资源。"""
    from taifeng_sandbox import SandboxError, SandboxUnavailableError
    from taifeng_sandbox.docker import DockerEnvironment
    from xuanpu_server.controlplane import InMemoryControlPlane
    from xuanpu_server.runtime.sandbox import DockerSandboxProvider

    created, active = [], set()
    allow_cleanup = False

    async def create(config, *, name=None):
        """模拟容器已启动，随后daemon握手失败且内部删除失败。"""
        created.append(name)
        active.add(name)
        raise SandboxUnavailableError("daemon unavailable; initial cleanup failed")

    async def remove(config, name):
        """保留故障，管理员修复Docker后才允许删除。"""
        if not allow_cleanup:
            raise SandboxError("docker rm failed")
        active.discard(name)

    monkeypatch.setattr(DockerEnvironment, "create", create)
    monkeypatch.setattr(DockerEnvironment, "remove", remove, raising=False)
    world = await world_opener(store=InMemoryControlPlane(),
        sandboxes=DockerSandboxProvider(image="python:3.12-slim", docker_env={}))
    try:
        await world.publish()
        package = await world.platform.packages.resolve("acme", "support-desk")
        with pytest.raises(Exception):
            await world.platform.runtimes.get(package)
        with pytest.raises(RuntimeUnavailableError):
            await world.platform.runtimes.get(package)
        assert len(created) == 1 and created[0] is not None
        with pytest.raises(ExceptionGroup):
            await world.platform.runtimes.close()
        assert active
        allow_cleanup = True
        await world.platform.runtimes.close()
        assert not active
    finally:
        allow_cleanup = True
        for name in list(active):
            await remove(None, name)
        await world.platform.close()
