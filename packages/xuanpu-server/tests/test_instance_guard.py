"""真实文件锁及数据库会话锁约束首期单进程启动。"""
from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import asyncpg
import pytest
from xuanpu_server.errors import ConflictError
from xuanpu_server.runtime.instance_guard import FileInstanceGuard, PostgresInstanceGuard


async def test_file_guard_excludes_second_owner(tmp_path: Path) -> None:
    """第二持有者被拒绝，释放后可以启动。"""
    first, second = FileInstanceGuard(tmp_path), FileInstanceGuard(tmp_path)
    await first.acquire()
    try:
        with pytest.raises(ConflictError):
            await second.acquire()
    finally:
        await first.close()
    await second.acquire()
    await second.close()


async def test_postgres_guard_excludes_owner_and_reports_loss(postgres_dsn: str) -> None:
    """实际 PostgreSQL 排他并在专用连接终止时立即标记不可用。"""
    connections = []
    async def connect() -> asyncpg.Connection:
        connection = await asyncpg.connect(postgres_dsn)
        connections.append(connection)
        return connection
    scope = "r0_" + secrets.token_hex(8)
    first, second = PostgresInstanceGuard(connect, scope), PostgresInstanceGuard(connect, scope)
    lost = asyncio.Event()
    first.on_lost = lost.set
    await first.acquire()
    try:
        with pytest.raises(ConflictError):
            await second.acquire()
        await connections[0].close()
        await asyncio.wait_for(lost.wait(), 2)
        assert first.lost
        await second.acquire()
    finally:
        await first.close()
        await second.close()


async def test_platform_acquires_before_background_workers(world: object, tmp_path: Path) -> None:
    """平台启动次序不能让第二个进程先启动后台工作再发现冲突。"""
    from typing import Any, cast
    item = cast("Any", world)
    directory = tmp_path / "one-lock"
    first = item.sibling("first", data_dir=tmp_path / "first", instance_guard=FileInstanceGuard(directory))
    second = item.sibling("second", data_dir=tmp_path / "second", instance_guard=FileInstanceGuard(directory))
    try:
        await first.start()
        with pytest.raises(ConflictError):
            await second.start()
    finally:
        await first.close()
        await second.close()


async def test_cleanup_failure_keeps_instance_guard(world: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无法确认工作已退出时不能允许另一个实例启动。"""
    from typing import Any, cast
    item = cast("Any", world)
    guard = FileInstanceGuard(tmp_path / "failure-lock")
    platform = item.sibling("failure", data_dir=tmp_path / "failure", instance_guard=guard)
    async def fail() -> None:
        raise RuntimeError("worker may still be running")
    await platform.start()
    monkeypatch.setattr(platform.proactive_worker, "close", fail)
    contender = FileInstanceGuard(tmp_path / "failure-lock")
    try:
        with pytest.raises(BaseExceptionGroup):
            await platform.close()
        with pytest.raises(ConflictError):
            await contender.acquire()
    finally:
        monkeypatch.undo()
        await platform.proactive_worker.close()
        await guard.close()
        await contender.close()


async def test_cancelled_file_acquisition_does_not_leak_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """后台文件操作晚于取消返回时，也不能留下无人管理的锁。"""
    import threading
    guard = FileInstanceGuard(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = guard._acquire
    def acquire() -> None:
        entered.set()
        release.wait(5)
        original()
    monkeypatch.setattr(guard, "_acquire", acquire)
    task = asyncio.create_task(guard.acquire())
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    contender = FileInstanceGuard(tmp_path)
    try:
        await contender.acquire()
    finally:
        await guard.close()
        await contender.close()


async def test_lock_connection_loss_rejects_http_and_stops_workers(world: object, postgres_dsn: str, tmp_path: Path) -> None:
    """实际锁连接终止后健康变503，清理完成前后均不能再接工作。"""
    from typing import Any, cast

    import httpx
    from xuanpu_server.api import create_app
    item = cast("Any", world)
    connections = []
    async def connect() -> asyncpg.Connection:
        connection = await asyncpg.connect(postgres_dsn)
        connections.append(connection)
        return connection
    guard = PostgresInstanceGuard(connect, "loss_" + secrets.token_hex(8))
    platform = item.sibling("loss", data_dir=tmp_path / "loss", instance_guard=guard)
    await platform.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(platform)), base_url="http://test") as client:
            assert (await client.get("/healthz")).status_code == 200
            notified = asyncio.Event()
            original_lost = guard.on_lost
            def lost() -> None:
                if original_lost is not None:
                    original_lost()
                notified.set()
            guard.on_lost = lost
            await connections[0].close()
            await asyncio.wait_for(notified.wait(), 2)
            assert guard.lost
            assert (await client.get("/healthz")).status_code == 503
            assert platform._failure_cleanup is not None
            await platform._failure_cleanup
            assert platform.sessions.held_sessions() == platform.runtimes.live_runtimes() == 0
            assert (await client.get("/healthz")).status_code == 503
    finally:
        await platform.close()


async def test_postgres_guard_excludes_separate_os_process(postgres_dsn: str) -> None:
    """另一个操作系统进程争同一库同一命名空间必须失败，释放后可获得。"""
    import sys
    scope = "process_" + secrets.token_hex(8)
    async def connect() -> asyncpg.Connection:
        return await asyncpg.connect(postgres_dsn)
    first = PostgresInstanceGuard(connect, scope)
    code = '''
import asyncio, asyncpg, sys
from xuanpu_server.runtime.instance_guard import PostgresInstanceGuard
from xuanpu_server.errors import ConflictError
async def main():
    async def connect():
        return await asyncpg.connect(sys.argv[1])
    guard = PostgresInstanceGuard(connect, sys.argv[2])
    try:
        await guard.acquire()
        print("acquired")
    except ConflictError:
        print("blocked")
    finally:
        await guard.close()
asyncio.run(main())
'''
    async def contender() -> str:
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, postgres_dsn, scope,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        output, error = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, error.decode()
        return output.decode().strip()
    await first.acquire()
    try:
        assert await contender() == "blocked"
    finally:
        await first.close()
    assert await contender() == "acquired"


async def test_lost_ownership_during_start_cannot_restart_workers(world: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """启动准备尚未结束就丢锁时，关闭后不能再启动后台工作。"""
    from typing import Any, cast
    item = cast("Any", world)
    platform = item.sibling("startup-loss", data_dir=tmp_path / "startup-loss")
    entered, release = asyncio.Event(), asyncio.Event()
    original = platform.packages.start
    async def prepare() -> None:
        entered.set()
        await release.wait()
        await original()
    monkeypatch.setattr(platform.packages, "start", prepare)
    start = asyncio.create_task(platform.start())
    await asyncio.wait_for(entered.wait(), 5)
    platform._ownership_lost()
    assert platform._failure_cleanup is not None
    await platform._failure_cleanup
    release.set()
    result = await asyncio.gather(start, return_exceptions=True)
    try:
        assert isinstance(result[0], Exception)
        assert platform.sessions._heartbeat is None
    finally:
        await platform.sessions.shutdown()
        await platform.proactive_worker.close()
        await platform.reminders.close()
        await platform.identity.maintenance.close()
        await platform.identity.disabling.close()
