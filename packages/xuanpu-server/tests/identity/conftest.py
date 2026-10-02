"""员工身份用例的公共夹具：带 HTTP 客户端的平台、模拟的飞书，外加建员工与登录会话的把手。

上一级 ``conftest.py`` 的夹具（``world_opener`` 等）在这里照样可用。测试模块之间不能互相 import，
共用的东西都经夹具提供。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from xuanpu_server.api import create_app
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.controlplane.models import AuditQuery
from xuanpu_server.identity import LoginSession, UserRecord, new_user_id
from xuanpu_server.identity.config import IdentityConfig
from xuanpu_server.identity.cookies import login_cookie_name, new_login_token
from xuanpu_server.identity.feishu import FeishuClient
from xuanpu_server.identity.service import IdentitySettings
from xuanpu_server.platform import ROOT_PRINCIPAL

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from xuanpu_server.controlplane.models import AuditRecord
    from xuanpu_server.identity import IdentityStore
    from xuanpu_server.tenancy import TenantRole

PUBLIC_URL = "https://xuanpu.example.com"
"""员工身份用例里平台的对外地址。"""
APP_ID = "cli_test_app"
"""模拟飞书里自建应用的 App ID。"""
APP_SECRET = "feishu-app-secret-PLAINTEXT"
"""模拟飞书认的 App Secret；用例写进保管库的明文。"""
ENCRYPT_KEY = "feishu-encrypt-key-PLAINTEXT"
VERIFICATION_TOKEN = "feishu-verification-PLAINTEXT"
SECRET_REFS = {
    "app_secret_ref": "feishu-app-secret",
    "encrypt_key_ref": "feishu-encrypt-key",
    "verification_token_ref": "feishu-verification-token",
}
"""飞书配置引用的三个密钥名。"""
_TOKEN_PATH = "/open-apis/authen/v2/oauth/token"
_USER_INFO_PATH = "/open-apis/authen/v1/user_info"


@dataclass(frozen=True)
class FeishuPerson:
    """模拟飞书里的一名员工。"""

    union_id: str
    open_id: str
    name: str
    tenant_key: str = "tk_acme"
    avatar_url: str | None = "https://avatar.example/a.png"


@dataclass
class FakeFeishu:
    """模拟的飞书：授权页（发授权码）、换令牌、取用户信息。

    换令牌时像真的飞书一样核对：App ID 与 App Secret、授权码只能用一次、``redirect_uri`` 与授权时
    一致、PKCE 校验串与 challenge 对得上。``fail`` 按接口（``token``、``user_info``）注入失败：值是
    响应就原样返回，是异常就抛出（模拟网络错误）。
    """

    accounts_base: str
    grants: dict[str, tuple[FeishuPerson, str, str]] = field(default_factory=dict)
    tokens: dict[str, FeishuPerson] = field(default_factory=dict)
    fail: dict[str, httpx.Response | Exception] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    @staticmethod
    def person(**fields: Any) -> FeishuPerson:
        """造一名飞书员工（用例拿不到本模块的类型，经这里造）。"""
        return FeishuPerson(**fields)

    @property
    def transport(self) -> httpx.MockTransport:
        """接到本模拟服务端的 httpx 传输。"""
        return httpx.MockTransport(self.handle)

    def authorize(self, location: str, person: FeishuPerson) -> tuple[str, str]:
        """员工在授权页点了同意：核对平台给的授权地址，发一个授权码。返回 ``(state, code)``。"""
        parts = urlsplit(location)
        assert f"{parts.scheme}://{parts.netloc}" == self.accounts_base
        assert parts.path == "/open-apis/authen/v1/authorize"
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        assert query["client_id"] == APP_ID
        assert (query["response_type"], query["code_challenge_method"]) == ("code", "S256")
        assert "scope" not in query
        code = "code-" + secrets.token_hex(8)
        self.grants[code] = (person, query["code_challenge"], query["redirect_uri"])
        return query["state"], code

    def handle(self, request: httpx.Request) -> httpx.Response:
        """按路径分派；先看有没有注入的失败。"""
        self.requests.append(request)
        name = {_TOKEN_PATH: "token", _USER_INFO_PATH: "user_info"}.get(request.url.path)
        if name is None:
            return httpx.Response(404, json={"code": 404, "msg": "not found"})
        injected = self.fail.get(name)
        if isinstance(injected, Exception):
            raise injected
        if injected is not None:
            return injected
        return self._token(request) if name == "token" else self._user_info(request)

    def _token(self, request: httpx.Request) -> httpx.Response:
        """换令牌：核对应用凭据、授权码、redirect_uri 与 PKCE。"""
        body = json.loads(request.content)
        if (body["client_id"], body["client_secret"]) != (APP_ID, APP_SECRET):
            return httpx.Response(400, json={"code": 20002, "error": "invalid_client"})
        grant = self.grants.pop(body["code"], None)
        if grant is None or body["grant_type"] != "authorization_code":
            return httpx.Response(400, json={"code": 20003, "error": "invalid_grant"})
        person, challenge, redirect_uri = grant
        digest = hashlib.sha256(body["code_verifier"].encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != challenge:
            return httpx.Response(400, json={"code": 20049, "error": "invalid_grant"})
        if body["redirect_uri"] != redirect_uri:
            return httpx.Response(400, json={"code": 20003, "error": "invalid_grant"})
        token = "u-" + secrets.token_hex(8)
        self.tokens[token] = person
        body = {"code": 0, "access_token": token, "expires_in": 7200, "token_type": "Bearer"}
        return httpx.Response(200, json=body)

    def _user_info(self, request: httpx.Request) -> httpx.Response:
        """取用户信息：按员工令牌找人。"""
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        person = self.tokens.get(token)
        if person is None:
            return httpx.Response(401, json={"code": 20005, "msg": "invalid access token"})
        data = {
            "name": person.name,
            "avatar_url": person.avatar_url or "",
            "open_id": person.open_id,
            "union_id": person.union_id,
            "tenant_key": person.tenant_key,
        }
        return httpx.Response(200, json={"code": 0, "msg": "success", "data": data})


@dataclass
class IdentityWorld:
    """装配好的平台（身份设置可调）、连到它的 HTTP 客户端，以及建员工与登录会话的把手。

    登录流程不经接口走：员工与登录会话直接经身份存储建，令牌用 ``new_login_token`` 签发。
    """

    world: Any
    """上一级 ``conftest`` 的 ``World``（测试模块之间不能互相 import，这里不写具体类型）。"""
    http: httpx.AsyncClient
    feishu: FakeFeishu
    """平台的飞书客户端接的模拟飞书。"""

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

    async def configure_feishu(self, *, session_ttl_seconds: int | None = None) -> None:
        """以租户 acme 管理员的身份写入飞书应用的三个密钥，再写飞书配置（不经 HTTP）。"""
        platform = self.world.platform
        admin = self.world.admin
        plaintexts = (APP_SECRET, ENCRYPT_KEY, VERIFICATION_TOKEN)
        for name, value in zip(SECRET_REFS.values(), plaintexts, strict=True):
            await platform.put_secret(admin, name, value)
        await platform.identity.put_feishu_config(
            admin, app_id=APP_ID, session_ttl_seconds=session_ttl_seconds, **SECRET_REFS
        )

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

    平台的飞书客户端换成接模拟飞书（``IdentityWorld.feishu``）的：上一级的 ``open_world`` 不带
    飞书传输的参数，这里在平台装配好之后替换登录流程用的客户端，原来那个先关掉。
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
            feishu = FakeFeishu(accounts_base=settings.feishu_accounts_base)
            login = world.platform.identity.login
            await login.feishu.aclose()
            login.feishu = FeishuClient(
                settings.feishu_open_base,
                settings.feishu_accounts_base,
                transport=feishu.transport,
                timeout_seconds=5.0,
            )
            transport = httpx.ASGITransport(
                app=create_app(world.platform), raise_app_exceptions=raise_app_exceptions
            )
            base_url = public_url or "http://xuanpu"
            async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
                yield IdentityWorld(world=world, http=http, feishu=feishu)
        finally:
            await world.platform.close()

    return opener
