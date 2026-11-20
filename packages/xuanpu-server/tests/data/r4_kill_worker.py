"""被父进程 SIGKILL 的知识登记工作进程（R4 故障注入）：真实 PostgreSQL 事务里写完来源与文本块后停住，
等父进程强制终止；只接受临时测试配置。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

import asyncpg
from taifeng_stores import SqlNamespace
from xuanpu_server.knowledge.store import Chunk, KnowledgeSource, PostgresKnowledgeStore, SourceRef


class _Paused:
    """连接代理：写完文本块（仍在事务里、未提交）后报告就绪并一直等待。"""

    def __init__(self, conn: Any, ready: Path) -> None:
        self._conn, self._ready = conn, ready

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def executemany(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._conn.executemany(*args, **kwargs)
        self._ready.write_text('in-transaction')
        await asyncio.Event().wait()
        return result


class _Pool:
    """连接池代理：取出的连接换成会停住的代理。"""

    def __init__(self, pool: asyncpg.Pool, ready: Path) -> None:
        self._pool, self._ready = pool, ready

    @contextlib.asynccontextmanager
    async def acquire(self) -> Any:
        async with self._pool.acquire() as conn:
            yield _Paused(conn, self._ready)


async def main() -> None:
    """按配置登记一个来源，停在提交前。"""
    config = json.loads(Path(sys.argv[1]).read_text())
    pool = await asyncpg.create_pool(config['dsn'], min_size=1, max_size=2)
    store = PostgresKnowledgeStore(_Pool(pool, Path(config['ready'])),  # type: ignore[arg-type]
                                   namespace=SqlNamespace(schema=config['schema'], prefix=config['prefix']))
    source = KnowledgeSource(
        tenant_id=config['tenant'], owner=config['owner'], id='ksr_killed',
        ref=SourceRef(kind='workspace_file', item_id=config['item'], revision=1), title='故障注入.txt',
        sha256='0' * 64, mime_type='text/plain', extractor='utf8-text/1', chars=6, chunks=1,
        created_at=1.0, updated_at=1.0)
    await store.add(source, [Chunk(source_id='ksr_killed', ordinal=0, text='故障注入正文')])


asyncio.run(main())
