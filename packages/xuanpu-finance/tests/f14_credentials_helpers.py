"""F14维护凭据边界的安全夹具和可终止特殊文件子进程。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xuanpu_finance.maintenance import (
    MaintenanceError,
    MaintenanceOptions,
    backup,
    maintenance_session,
    restore,
)

if TYPE_CHECKING:
    import pytest

SAFE_DSN = 'postgresql://f14_user:f14_password@localhost:5432/finance_test_f14'
SAFE_KEY = b'f'*32


def options_for(root: Path, mode: int = 0o600) -> MaintenanceOptions:
    """只创建安全哨兵凭据，不包含真实数据库秘密。"""
    dsn = root/'dsn'
    key = root/'key'
    dsn.write_text(SAFE_DSN)
    key.write_bytes(SAFE_KEY)
    dsn.chmod(mode)
    key.chmod(mode)
    return MaintenanceOptions(dsn, root/'objects', key)


def invalidate(path: Path, kind: str) -> None:
    """替换叶子文件，保留其它测试输入的私密权限。"""
    if kind == 'permissions':
        path.chmod(0o644)
    elif kind == 'symlink':
        moved = path.with_name(path.name+'-target')
        path.rename(moved)
        path.symlink_to(moved)
    elif kind == 'directory':
        path.unlink()
        path.mkdir()
    else:
        values = {'empty': b'', 'oversize': b'x'*65537, 'utf8': b'\xff', 'short': b'x'*31}
        path.write_bytes(values[kind])


async def invoke(action: str, options: MaintenanceOptions, package: Path) -> None:
    """三个公开维护入口共用同一拒绝断言。"""
    if action == 'session':
        async with maintenance_session(options):
            raise AssertionError('unsafe input accepted')
    elif action == 'backup':
        await backup(options, package)
    else:
        await restore(options, package)


def forbidden_boundaries(patch: pytest.MonkeyPatch) -> dict[str, int]:
    """记录连接、工具和暂存目录边界，坏输入必须全部零次。"""
    import xuanpu_finance.maintenance as module
    calls = {'connect': 0, 'tool': 0, 'stage': 0}

    async def connect(*args: Any, **kwargs: Any) -> Any:
        """任何数据库连接尝试立即失败，不消费外部资源。"""
        calls['connect'] += 1
        raise AssertionError('unexpected database boundary')

    async def tool(*args: Any, **kwargs: Any) -> Any:
        """坏凭据不得启动任何工具。"""
        calls['tool'] += 1
        raise AssertionError('unexpected tool boundary')

    def stage(*args: Any, **kwargs: Any) -> Any:
        """坏凭据不得创建任何暂存目录。"""
        calls['stage'] += 1
        raise AssertionError('unexpected stage boundary')

    patch.setattr(module.asyncpg, 'connect', connect)
    patch.setattr(asyncio, 'create_subprocess_exec', tool)
    patch.setattr(module.tempfile, 'mkdtemp', stage)
    return calls


class Connection:
    """合法捕获单测只模拟维护会话协议，不冒充真实PG验证。"""

    def __init__(self) -> None:
        """记录会话关闭，供异常路径断言。"""
        self.closed = False

    async def fetchval(self, *args: Any) -> bool:
        """维护锁单测允许持锁。"""
        return True

    def add_termination_listener(self, callback: Any) -> None:
        """本地协议替身无需后台监听器。"""
        self.callback = callback

    async def close(self) -> None:
        """记录持锁连接总会关闭。"""
        self.closed = True


async def special_probe() -> None:
    """子进程只报告固定失败码和边界计数，绝不输出异常正文。"""
    import pytest
    patch = pytest.MonkeyPatch()
    calls = forbidden_boundaries(patch)
    options = MaintenanceOptions(Path(sys.argv[1]), Path(sys.argv[3]), Path(sys.argv[2]))
    try:
        await invoke(sys.argv[4], options, Path(sys.argv[5]))
        code = 'ACCEPTED'
    except MaintenanceError as error:
        code = str(error)
    except Exception:
        code = 'UNEXPECTED'
    finally:
        patch.undo()
    print(json.dumps({'code': code, 'calls': calls}))


if __name__ == '__main__':
    asyncio.run(special_probe())
