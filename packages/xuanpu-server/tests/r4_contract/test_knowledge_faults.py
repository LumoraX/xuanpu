"""知识登记的真实进程故障（R4-D1 故障注入，仅 PostgreSQL）：独立进程在事务里写完来源与文本块、
提交前被 SIGKILL，数据库里既没有来源也没有半截文本块；同一材料随后正常登记一次。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from xuanpu_server.knowledge.service import KnowledgeConfig

WORKER = Path(__file__).resolve().parents[1] / 'data' / 'r4_kill_worker.py'


async def test_sigkill_inside_registration_leaves_nothing(r4_http, r4_world, postgres_dsn, tmp_path):
    """事务未提交即进程死亡：来源与文本块都不存在；之后经接口登记成功且只有一份。"""
    store = r4_world.platform.knowledge.store
    if not hasattr(store, '_pool'):
        pytest.skip('只在 PostgreSQL 后端验证真实进程终止')
    r4_world.platform.knowledge.config = KnowledgeConfig(enabled=True, tenants=('acme',))
    alice = r4_http['alice']
    uploaded = await alice.post('/v1/workspace/files', params={'name': '故障注入.txt', 'request_id': 'fault-src'},
                                content='故障注入正文'.encode(), headers={'Content-Type': 'text/plain'})
    item = uploaded.json()['id']
    namespace = r4_world.platform.store._namespace
    ready, config = tmp_path / 'ready', tmp_path / 'kill.json'
    config.write_text(json.dumps({'dsn': postgres_dsn, 'schema': namespace.schema, 'prefix': namespace.prefix,
                                  'tenant': 'acme', 'owner': 'usr_alice', 'item': item, 'ready': str(ready)}))
    config.chmod(0o600)
    process = await asyncio.create_subprocess_exec(sys.executable, str(WORKER), str(config),
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(30):
            while not ready.exists():
                if process.returncode is not None:
                    _, error = await process.communicate()
                    pytest.fail(error.decode()[-4000:])
                await asyncio.sleep(0.05)
    finally:
        process.kill()
        await process.wait()
    assert process.returncode is not None and process.returncode < 0, '须是被信号终止，而不是正常退出'
    assert await store.sources('acme', 'usr_alice') == []
    async with store._pool.acquire() as conn:
        left = await conn.fetchval(f'SELECT count(*) FROM {store._chunks} WHERE tenant_id=$1', 'acme')
    assert left == 0, '提交前死亡不能留下半截文本块'
    registered = await alice.post('/v1/me/knowledge/sources', json={'item_id': item, 'revision': 1})
    assert registered.status_code == 201, registered.text
    assert len(await store.sources('acme', 'usr_alice')) == 1
