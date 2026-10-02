"""员工身份用例的公共夹具：带 HTTP 客户端的平台，外加建员工与登录会话的把手。

上一级 ``conftest.py`` 的夹具（``world_opener`` 等）在这里照样可用。测试模块之间不能互相 import，
共用的东西都经夹具提供。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from xuanpu_server.api import create_app
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.controlplane.models import AuditQuery
from xuanpu_server.identity import LoginSession, UserRecord, new_user_id
from xuanpu_server.identity.config import IdentityConfig
from xuanpu_server.identity.cookies import login_cookie_name, new_login_token
from xuanpu_server.identity.service import IdentitySettings
from xuanpu_server.platform import ROOT_PRINCIPAL

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from xuanpu_server.controlplane.models import AuditRecord
    from xuanpu_server.identity import IdentityStore
    from xuanpu_server.tenancy import TenantRole

PUBLIC_URL = "https://xuanpu.example.com"
"""员工身份用例里平台的对外地址。"""


@dataclass
class IdentityWorld:
    """装配好的平台（身份设置可调）、连到它的 HTTP 客户端，以及建员工与登录会话的把手。

    登录流程不经接口走：员工与登录会话直接经身份存储建，令牌用 ``new_login_token`` 签发。
    """

    world: Any
    """上一级 ``conftest`` 的 ``World``（测试模块之间不能互相 import，这里不写具体类型）。"""
    http: httpx.AsyncClient

    @property
    def store(self) -> IdentityStore:
        """平台的身份存储。"""
        store: IdentityStore = self.world.platform.identity.store
        return store

    @property
    def cookie_name(self) -> str:
        """按平台配置，登录 cookie 的名字。"""
        return login_cookie_name(self.secure)

    @property
    def secure(self) -> bool:
        """平台配置的 ``identity.cookie_secure``。"""
        secure: bool = self.world.platform.identity.settings.cookie_secure
        return secure

    def cookie(self, token: str) -> dict[str, str]:
        """带登录 cookie 的请求头。"""
        return {"Cookie": f"{self.cookie_name}={token}"}

    def cleared(self, response: httpx.Response) -> bool:
        """响应是否让浏览器丢掉登录 cookie：名字与属性和写入时一致（Secure 随配置）。"""
        header = response.headers.get("set-cookie", "")
        parts = {part.strip().lower() for part in header.split(";")}
        expected = {"max-age=0", "path=/", "httponly", "samesite=lax"}
        return (
            header.startswith(f'{self.cookie_name}=""')
            and expected <= parts
            and ("secure" in parts) == self.secure
            and not any(part.startswith("domain=") for part in parts)
        )

    async def user(
        self, *, role: TenantRole = "tenant_member", tenant: str = "acme", name: str = "张三"
    ) -> UserRecord:
        """建一名启用的员工。"""
        user = UserRecord(
            tenant_id=tenant,
            user_id=new_user_id(),
            display_name=name,
            created_at=time.time(),
            role=role,
        )
        await self.store.create_user(user)
        return user

    async def login(
        self, user: UserRecord, *, expires_in: float = 3600.0, last_seen_ago: float = 0.0
    ) -> str:
        """给员工建一个登录会话，返回 cookie 里的令牌。"""
        token, token_hash = new_login_token()
        now = time.time()
        await self.store.create_login_session(
            LoginSession(
                token_hash=token_hash,
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                created_at=now - last_seen_ago,
                expires_at=now + expires_in,
                last_seen_at=now - last_seen_ago,
            )
        )
        return token

    async def system_key(self, *, role: TenantRole = "tenant_admin", tenant: str = "acme") -> str:
        """签发一把能代员工调用的 API key。"""
        issued = await self.world.platform.issue_key(
            ROOT_PRINCIPAL, tenant_id=tenant, name="system", role=role, act_as_users=True
        )
        key: str = issued.key
        return key

    async def audit(self, kind: str) -> list[AuditRecord]:
        """租户 acme 里某种审计记录。"""
        records: list[AuditRecord] = await self.world.platform.store.query_audit(
            "acme", AuditQuery(kind=kind)
        )
        return records


@pytest.fixture
def identity_world(world_opener: Any, local_sandboxes: Any) -> Any:
    """返回 ``opener(public_url=…, raise_app_exceptions=…, **identity段)`` 异步上下文管理器。

    装配内存控制面上的平台（租户 acme，一把管理员 key、一把成员 key），身份设置由默认配置加上
    给定的 ``identity`` 段各项构造；退出时关掉平台。``public_url`` 默认 ``PUBLIC_URL``，给 None
    就是没配。每个用例只能开一个（数据目录按用例绑定）。
    """

    @asynccontextmanager
    async def opener(
        *, public_url: str | None = PUBLIC_URL, raise_app_exceptions: bool = True, **identity: Any
    ) -> AsyncIterator[IdentityWorld]:
        """装配平台并连上 HTTP 客户端。"""
        settings = IdentitySettings.from_config(public_url, IdentityConfig(**identity))
        world = await world_opener(
            store=InMemoryControlPlane(), sandboxes=local_sandboxes(), identity_settings=settings
        )
        try:
            transport = httpx.ASGITransport(
                app=create_app(world.platform), raise_app_exceptions=raise_app_exceptions
            )
            base_url = public_url or "http://xuanpu"
            async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
                yield IdentityWorld(world=world, http=http)
        finally:
            await world.platform.close()

    return opener
