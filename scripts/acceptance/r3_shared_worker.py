"""隔离合成库上的真实进程故障点；只由显式验收配置启动。"""
from __future__ import annotations

import argparse
import asyncio
import json
import stat
from pathlib import Path
from typing import Any

import asyncpg
from taifeng_stores import SqlNamespace
from xuanpu_server.controlplane.postgres import PostgresControlPlane
from xuanpu_server.coordination.postgres import PostgresCoordinationStore
from xuanpu_server.identity.postgres import PostgresIdentityStore
from xuanpu_server.objectstore import LocalObjectStore
from xuanpu_server.organization.service import OrganizationService
from xuanpu_server.shared_matters.context_guard import SharedContexts
from xuanpu_server.shared_matters.materials import SharedMaterials
from xuanpu_server.shared_matters.models import SharedMatterConfig
from xuanpu_server.shared_matters.service import SharedMatterService
from xuanpu_server.tenancy import Principal
from xuanpu_server.workspace.postgres import PostgresWorkspaceStore


def config_file(path: Path) -> dict[str, Any]:
    """数据库配置只能位于权限600普通文件，绝不写入stdout。"""
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError('需要权限600的私有配置')
    config = json.loads(path.read_text())
    if not config['schema'].startswith('t_') or config['tenant'] != 'acme':
        raise ValueError('仅允许独占的合成测试命名空间')
    return config


class PausedObjects(LocalObjectStore):
    """真实对象已持久落盘、发布提交尚未发生时停止，等待父进程SIGKILL。"""
    def __init__(self, root: Path, marker: Path) -> None:
        """故障点与对象目录只由私有合成验收配置提供。"""
        super().__init__(root)
        self.marker = marker

    async def put(self, key: str, data: bytes, *, content_type: str = 'application/octet-stream') -> None:
        """公开材料前的真实字节写入，标记不包含正文和数据库凭据。"""
        await super().put(key, data, content_type=content_type)
        if '/shared/' in key:
            await asyncio.to_thread(self.marker.write_text, json.dumps({'phase': 'copied_before_commit'}))
            await asyncio.Future()


async def run(config: dict[str, Any]) -> None:
    """复用正式协调事务及发布服务，独立进程不共享父进程内存。"""
    pool = await asyncpg.create_pool(config['dsn'], min_size=1, max_size=4)
    namespace = SqlNamespace(schema=config['schema'], prefix=config['prefix'])
    control = PostgresControlPlane(pool, namespace=namespace)
    identity = PostgresIdentityStore(pool, namespace=namespace)
    coordination = PostgresCoordinationStore(pool, namespace)
    organization = OrganizationService(coordination, identity, control)
    service = SharedMatterService(organization, config=SharedMatterConfig(enabled=True, tenants=('acme',)))
    objects = PausedObjects(Path(config['objects_dir']), Path(config['marker']))
    materials = SharedMaterials(service, PostgresWorkspaceStore(pool, namespace=namespace), objects)
    who = Principal(role='tenant_member', tenant_id=config['tenant'], user_id=config['user_id'], key_id=None)
    try:
        if config['action'] == 'copy':
            await materials.upload(who, config['shared_id'], request_id='sigkill-copy', expected=config['version'],
                name='故障验收.txt', mime='text/plain', data=b'isolated-durable-copy-proof')
        else:
            from xuanpu_server.errors import PlatformError
            try:
                await SharedContexts(materials).read(who, config['context_id'])
            except PlatformError as exc:
                print(json.dumps({'code': exc.code}), flush=True)
            else:
                raise RuntimeError('撤权上下文意外恢复')
    finally:
        await pool.close()


def main() -> None:
    """参数只包含私有配置路径，不接受数据库连接串或真实业务覆盖。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(config_file(args.config)))


if __name__ == '__main__':
    main()
