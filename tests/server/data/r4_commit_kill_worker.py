"""被父进程 SIGKILL 的提交窗口工作进程（R4 故障注入，仅 PostgreSQL）。

用真实的 PG 存储与真实的能力服务代码（发布、停止/撤销发布、撤销分配、验收观察、调用绑定），
只把连接池换成会在提交窗口停住的代理：

- 协调事务（``stage=before``）：事务体与全部写入都已执行、``COMMIT`` 尚未发出时报告就绪并阻塞；
- 协调事务（``stage=after``）：``COMMIT`` 已完成、结果尚未返回给调用方时报告就绪并阻塞（模拟响应丢失）；
- 调用绑定（``mode=binding``）：会话行已提交、绑定的单语句插入发出之前报告就绪并阻塞。

阻塞用同步 ``time.sleep``：事件循环被占住，协调事务自带的超时无法触发回滚，进程只能被信号终止。
只接受父进程写的临时测试配置。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
from taifeng_stores import SqlNamespace

from server.core.controlplane.models import SessionRecord
from server.core.controlplane.postgres import PostgresControlPlane
from server.core.tenancy import Principal
from server.domains.capabilities.config import CapabilityDeliveryConfig
from server.domains.capabilities.resolver import CapabilityDecision
from server.domains.capabilities.schemas import ReleaseInput, ReleaseStateInput, RevokeInput
from server.domains.capabilities.service import CapabilityService
from server.domains.identity.postgres import PostgresIdentityStore
from server.domains.organization.service import OrganizationService
from server.domains.outcomes.acceptance import AcceptanceService, ObservationInput


@dataclass
class _Window:
    """停在哪个窗口：``before`` / ``after`` 协调事务提交，或 ``binding`` 绑定插入之前。"""

    stage: str
    ready: Path

    def hold(self, where: str) -> None:
        """报告就绪并阻塞到被杀；不返回。"""
        self.ready.write_text(where)
        time.sleep(3600)
        raise SystemExit('父进程未在时限内终止工作进程')


class _Transaction:
    """写事务代理：按窗口在提交前或提交后停住；只读事务与异常回滚照常。"""

    def __init__(self, inner: Any, window: _Window, readonly: bool) -> None:
        self._inner, self._window, self._readonly = inner, window, readonly

    async def __aenter__(self) -> Any:
        return await self._inner.__aenter__()

    async def __aexit__(self, kind: Any, error: Any, trace: Any) -> Any:
        if kind is not None or self._readonly or self._window.stage == 'binding':
            return await self._inner.__aexit__(kind, error, trace)
        if self._window.stage == 'before':
            self._window.hold('before-commit')
        result = await self._inner.__aexit__(kind, error, trace)
        self._window.hold('after-commit')
        return result


class _Connection:
    """连接代理：事务换成会停住的代理；绑定模式下在绑定插入之前停住。"""

    def __init__(self, conn: Any, window: _Window) -> None:
        self._conn, self._window = conn, window

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def transaction(self, **kwargs: Any) -> _Transaction:
        return _Transaction(self._conn.transaction(**kwargs), self._window, bool(kwargs.get('readonly')))

    async def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        if self._window.stage == 'binding' and 'capability_binding' in sql:
            self._window.hold('before-binding')
        return await self._conn.execute(sql, *args, **kwargs)


class _Pool:
    """连接池代理：取出的连接都换成代理；其余属性照转。"""

    def __init__(self, pool: asyncpg.Pool, window: _Window) -> None:
        self._pool, self._window = pool, window

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)

    @contextlib.asynccontextmanager
    async def acquire(self) -> Any:
        async with self._pool.acquire() as conn:
            yield _Connection(conn, self._window)


def _principal(config: dict[str, Any]) -> Principal:
    """配置里的员工身份（管理员或员工）。"""
    return Principal(role=config['role'], tenant_id=config['tenant'], user_id=config['user'], key_id=None)


async def _binding(control: PostgresControlPlane, service: CapabilityService, config: dict[str, Any]) -> None:
    """照 ``SessionService.create`` 的顺序：先提交会话行，再写调用绑定（停在绑定之前）。"""
    who = _principal(config)
    decision = CapabilityDecision(**config['decision'])
    now = time.time()
    await control.create_session(SessionRecord(
        session_id=config['session_id'], tenant_id=config['tenant'], package=decision.package_name,
        version=decision.package_version, entry=decision.entry, created_at=now, updated_at=now,
        created_by=who.actor, metadata={}, user_id=who.user_id, contains_external_input=True))
    await service.admission.bind(who, 'session', config['session_id'], decision)


async def main() -> None:
    """按配置装配真实服务，执行一次写操作并停在指定窗口。"""
    config = json.loads(Path(sys.argv[1]).read_text())
    window = _Window(stage=config['stage'], ready=Path(config['ready']))
    raw = await asyncpg.create_pool(config['dsn'], min_size=1, max_size=4)
    pool: Any = _Pool(raw, window)
    namespace = SqlNamespace(schema=config['schema'], prefix=config['prefix'])
    control = PostgresControlPlane(pool, namespace=namespace)
    identity = PostgresIdentityStore(pool, namespace=namespace)
    coordination = control.coordination_store
    coordination.bind_identity(identity)
    organization = OrganizationService(coordination, identity, control)
    service = CapabilityService(organization, None,  # type: ignore[arg-type]  # 这些写操作不读包仓库
                                CapabilityDeliveryConfig(enabled=True, tenants=(config['tenant'],)),
                                preflight=None)  # type: ignore[arg-type]  # 不做安装预检
    who, body, mode = _principal(config), config.get('body', {}), config['mode']
    if mode == 'binding':
        await _binding(control, service, config)
    elif mode == 'release':
        await service.releases.release(who, config['capability_id'], ReleaseInput.model_validate(body))
    elif mode == 'release_revoke':
        await service.releases.change(who, config['release_id'], ReleaseStateInput.model_validate(body), 'revoked')
    elif mode == 'assignment_revoke':
        await service.assignments.revoke(who, config['assignment_id'], RevokeInput.model_validate(body))
    elif mode == 'observe':
        acceptance = AcceptanceService(service, control.binding_store)
        await acceptance.observe(who, config['batch_id'], ObservationInput.model_validate(body))
    else:
        raise SystemExit('未知模式：' + mode)
    raise SystemExit('写操作返回了：窗口没有被触发')


asyncio.run(main())
