"""调用者身份与认证入口：API key、浏览器登录 cookie、系统代员工调用、防跨站请求伪造。

经真实的 ASGI 应用（``httpx.ASGITransport``）走请求。登录流程还没有（P1 后续任务），登录会话在
用例里直接经身份存储建：令牌用 ``new_login_token`` 签发，库里只存摘要。
"""

from __future__ import annotations

import json
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import asyncpg
import httpx
import pytest
from starlette.responses import Response
from taifeng_stores import SqlNamespace
from xuanpu_server.api import create_app
from xuanpu_server.bootstrap import open_platform
from xuanpu_server.config import parse_config
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.controlplane.models import AuditQuery
from xuanpu_server.controlplane.schema import apply_migrations
from xuanpu_server.errors import CsrfRejectedError, PermissionDeniedError
from xuanpu_server.identity import InMemoryIdentityStore, LoginSession, UserRecord, new_user_id
from xuanpu_server.identity.cookies import (
    check_cookie_request,
    clear_login_cookie,
    login_token_hash,
    new_login_token,
    set_login_cookie,
)
from xuanpu_server.identity.postgres import PostgresIdentityStore
from xuanpu_server.identity.service import LAST_SEEN_INTERVAL_SECONDS, IdentitySettings
from xuanpu_server.platform import ROOT_PRINCIPAL
from xuanpu_server.tenancy import Principal, lower_role

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from xuanpu_server.identity import IdentityStore
    from xuanpu_server.tenancy import TenantRole

PUBLIC_URL = "https://xuanpu.example.com"
WRITE = {"X-Requested-By": "xuanpu"}
UNAUTHENTICATED = {"error": {"code": "unauthenticated", "message": "凭据无效"}}


def bearer(key: str) -> dict[str, str]:
    """带 API key 的请求头。"""
    return {"Authorization": f"Bearer {key}"}


def cookie(token: str) -> dict[str, str]:
    """带登录 cookie 的请求头。"""
    return {"Cookie": f"xuanpu_session={token}"}


def act_as(key: str, user: UserRecord) -> dict[str, str]:
    """用 API key 代某名员工调用的请求头。"""
    return {**bearer(key), "X-Xuanpu-Act-As": user.user_id}


@dataclass
class Ident:
    """装配好的平台（配了 ``public_url``）、连到它的 HTTP 客户端，以及建员工与登录会话的把手。"""

    world: Any
    http: httpx.AsyncClient

    @property
    def store(self) -> IdentityStore:
        """平台的身份存储。"""
        store: IdentityStore = self.world.platform.identity.store
        return store

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

    async def audit(self, kind: str) -> list[Any]:
        """租户 acme 里某种控制面审计记录。"""
        return list(await self.world.platform.store.query_audit("acme", AuditQuery(kind=kind)))


@asynccontextmanager
async def _ident(
    world_opener: Any, local_sandboxes: Any, settings: IdentitySettings
) -> AsyncIterator[Ident]:
    """装配平台并连上 HTTP 客户端；退出时关掉平台。"""
    world = await world_opener(
        store=InMemoryControlPlane(), sandboxes=local_sandboxes(), identity_settings=settings
    )
    try:
        transport = httpx.ASGITransport(app=create_app(world.platform))
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL) as http:
            yield Ident(world=world, http=http)
    finally:
        await world.platform.close()


@pytest.fixture
async def ident(world_opener: Any, local_sandboxes: Any) -> AsyncIterator[Ident]:
    """配了 ``public_url`` 的平台：租户 acme，一把管理员 key、一把成员 key。"""
    settings = IdentitySettings(public_url=PUBLIC_URL)
    async with _ident(world_opener, local_sandboxes, settings) as item:
        yield item


@pytest.fixture
async def no_public_url(world_opener: Any, local_sandboxes: Any) -> AsyncIterator[Ident]:
    """没配 ``public_url`` 的平台。"""
    async with _ident(world_opener, local_sandboxes, IdentitySettings()) as item:
        yield item


# --- 调用者身份 ---


@pytest.mark.parametrize(
    ("principal", "actor"),
    [
        (Principal(role="platform_admin", tenant_id=None, key_id="root"), "key:root"),
        (Principal(role="tenant_admin", tenant_id="acme", key_id="k1"), "key:k1"),
        (
            Principal(role="tenant_member", tenant_id="acme", key_id=None, user_id="usr_1"),
            "user:usr_1",
        ),
        (
            Principal(role="tenant_member", tenant_id="acme", key_id="k1", user_id="usr_1"),
            "key:k1/user:usr_1",
        ),
    ],
)
def test_actor_of_each_caller(principal: Principal, actor: str) -> None:
    """``actor`` 的四种写法（设计 5.2）。"""
    assert principal.actor == actor


def test_principal_must_name_a_key_or_a_user() -> None:
    """调用者总要落到一把 key 或一名员工上；员工总属于某个租户。"""
    with pytest.raises(ValueError, match="key_id 或 user_id"):
        Principal(role="tenant_member", tenant_id="acme", key_id=None)
    with pytest.raises(ValueError, match="租户"):
        Principal(role="tenant_member", tenant_id=None, key_id=None, user_id="usr_1")


@pytest.mark.parametrize(
    ("first", "second", "lower"),
    [
        ("tenant_admin", "tenant_member", "tenant_member"),
        ("tenant_member", "tenant_admin", "tenant_member"),
        ("tenant_admin", "tenant_admin", "tenant_admin"),
        ("platform_admin", "tenant_admin", "tenant_admin"),
    ],
)
def test_lower_role(first: Any, second: Any, lower: str) -> None:
    """两个角色里取权限较低的。"""
    assert lower_role(first, second) == lower


async def test_each_caller_is_recorded_in_audit(ident: Ident) -> None:
    """三种调用者写的控制面审计：``actor`` 各按其形式，``user_id`` 是代表的员工（key 为空）。"""
    admin = await ident.user(role="tenant_admin")
    system = await ident.system_key()
    calls = [
        ("by-key", bearer(ident.world.admin_key)),
        ("by-cookie", {**cookie(await ident.login(admin)), **WRITE}),
        ("by-act-as", act_as(system, admin)),
    ]
    for name, headers in calls:
        response = await ident.http.put(f"/v1/secrets/{name}", headers=headers, json={"value": "v"})
        assert response.status_code == 200, response.text

    records = {record.data["name"]: record for record in await ident.audit("secret_written")}
    admin_key_id = ident.world.admin.key_id
    system_key_id = system.split("_")[1]
    assert (records["by-key"].actor, records["by-key"].user_id) == (f"key:{admin_key_id}", None)
    assert (records["by-cookie"].actor, records["by-cookie"].user_id) == (
        f"user:{admin.user_id}",
        admin.user_id,
    )
    assert (records["by-act-as"].actor, records["by-act-as"].user_id) == (
        f"key:{system_key_id}/user:{admin.user_id}",
        admin.user_id,
    )


# --- GET /v1/me ---


async def test_me_for_each_caller(ident: Ident) -> None:
    """``GET /v1/me``：API key、浏览器登录、代员工调用、根凭据，同一个形状。"""
    member = await ident.user(name="李四")
    system = await ident.system_key()
    by_key = await ident.http.get("/v1/me", headers=bearer(ident.world.admin_key))
    by_cookie = await ident.http.get("/v1/me", headers=cookie(await ident.login(member)))
    by_act_as = await ident.http.get("/v1/me", headers=act_as(system, member))
    by_root = await ident.http.get("/v1/me", headers=bearer(ident.world.root_key))

    admin_key_id = ident.world.admin.key_id
    system_key_id = system.split("_")[1]
    assert by_key.json() == {
        "actor": f"key:{admin_key_id}",
        "role": "tenant_admin",
        "tenant_id": "acme",
        "key_id": admin_key_id,
        "user_id": None,
        "display_name": None,
    }
    assert by_cookie.json() == {
        "actor": f"user:{member.user_id}",
        "role": "tenant_member",
        "tenant_id": "acme",
        "key_id": None,
        "user_id": member.user_id,
        "display_name": "李四",
    }
    # 管理员的 key 代成员调用：生效角色是成员
    assert by_act_as.json() == {
        "actor": f"key:{system_key_id}/user:{member.user_id}",
        "role": "tenant_member",
        "tenant_id": "acme",
        "key_id": system_key_id,
        "user_id": member.user_id,
        "display_name": "李四",
    }
    assert by_root.json() == {
        "actor": "key:root",
        "role": "platform_admin",
        "tenant_id": None,
        "key_id": "root",
        "user_id": None,
        "display_name": None,
    }


# --- 浏览器登录 ---


def _cleared(response: httpx.Response) -> bool:
    """响应是否让浏览器丢掉登录 cookie（属性与写入时一致）。"""
    header = response.headers.get("set-cookie", "")
    parts = {part.strip().lower() for part in header.split(";")}
    expected = {"max-age=0", "path=/", "httponly", "samesite=lax", "secure"}
    return header.startswith('xuanpu_session=""') and expected <= parts


async def test_login_cookie_authenticates(ident: Ident) -> None:
    """有效的登录 cookie 认出员工；不带 ``Authorization`` 也能调租户接口。"""
    member = await ident.user()
    response = await ident.http.get("/v1/sessions", headers=cookie(await ident.login(member)))
    assert response.status_code == 200
    assert response.json() == {"sessions": []}
    assert "set-cookie" not in response.headers


async def _expire(ident: Ident, user: UserRecord) -> str:
    """登录会话已过期。"""
    return await ident.login(user, expires_in=-1)


async def _disable_user(ident: Ident, user: UserRecord) -> str:
    """员工已停用。"""
    token = await ident.login(user)
    await ident.store.set_user_status(
        user.tenant_id, user.user_id, "disabled", reason="admin", at=time.time()
    )
    return token


async def _suspend_tenant(ident: Ident, user: UserRecord) -> str:
    """租户已停用。"""
    token = await ident.login(user)
    await ident.world.platform.set_tenant_status(ROOT_PRINCIPAL, user.tenant_id, "suspended")
    return token


async def _unknown(ident: Ident, user: UserRecord) -> str:
    """库里没有这个令牌。"""
    return new_login_token()[0]


@pytest.mark.parametrize("spoil", [_expire, _disable_user, _suspend_tenant, _unknown])
async def test_invalid_login_cookie_is_rejected_and_cleared(ident: Ident, spoil: Any) -> None:
    """会话过期、员工停用、租户停用、令牌不存在：同一个 401，并清掉 cookie。"""
    token = await spoil(ident, await ident.user())
    response = await ident.http.get("/v1/me", headers=cookie(token))
    assert response.status_code == 401
    assert response.json() == UNAUTHENTICATED
    assert _cleared(response)


async def test_disabling_takes_effect_on_the_next_request(ident: Ident) -> None:
    """不缓存：同一个 cookie，员工一停用，下一个请求就被拒。"""
    member = await ident.user()
    token = await ident.login(member)
    assert (await ident.http.get("/v1/me", headers=cookie(token))).status_code == 200
    await ident.store.set_user_status(
        "acme", member.user_id, "disabled", reason="admin", at=time.time()
    )
    assert (await ident.http.get("/v1/me", headers=cookie(token))).status_code == 401


async def test_no_credentials_at_all(ident: Ident) -> None:
    """既没有 ``Authorization`` 也没有 cookie：401，不发清 cookie 的头。"""
    response = await ident.http.get("/v1/me")
    assert response.status_code == 401
    assert response.json() == UNAUTHENTICATED
    assert "set-cookie" not in response.headers


async def test_authorization_header_wins_over_cookie(ident: Ident) -> None:
    """带了 ``Authorization`` 就按 key 认证，不看 cookie：key 无效就是 401，即使 cookie 有效。"""
    member = await ident.user()
    token = await ident.login(member)
    by_key = await ident.http.get(
        "/v1/me", headers={**cookie(token), **bearer(ident.world.admin_key)}
    )
    assert by_key.json()["actor"] == f"key:{ident.world.admin.key_id}"
    bad_key = await ident.http.get("/v1/me", headers={**cookie(token), **bearer("xp_nope")})
    assert bad_key.status_code == 401
    assert "set-cookie" not in bad_key.headers


async def test_last_seen_is_written_at_most_every_five_minutes(ident: Ident) -> None:
    """``last_seen_at`` 距上次写入不到 5 分钟不写，满 5 分钟才写。"""
    member = await ident.user()
    recent = await ident.login(member, last_seen_ago=LAST_SEEN_INTERVAL_SECONDS - 60)
    stale = await ident.login(member, last_seen_ago=LAST_SEEN_INTERVAL_SECONDS + 60)
    before = await _last_seen(ident, recent), await _last_seen(ident, stale)
    started = time.time()
    for token in (recent, stale):
        assert (await ident.http.get("/v1/me", headers=cookie(token))).status_code == 200
    assert await _last_seen(ident, recent) == before[0]
    assert await _last_seen(ident, stale) >= started


async def _last_seen(ident: Ident, token: str) -> float:
    """登录会话当前的 ``last_seen_at``。"""
    session = await ident.store.get_login_session(login_token_hash(token), now=time.time())
    assert session is not None
    return session.last_seen_at


def test_cookie_attributes() -> None:
    """登录 cookie：HttpOnly、SameSite=Lax、Path=/，Secure 由配置决定；清除时属性相同。"""
    response = Response()
    set_login_cookie(response, "tok", max_age=600, secure=True)
    set_login_cookie(response, "tok", max_age=600, secure=False)
    clear_login_cookie(response, secure=True)
    written, insecure, cleared = response.headers.getlist("set-cookie")
    assert written == "xuanpu_session=tok; HttpOnly; Max-Age=600; Path=/; SameSite=lax; Secure"
    assert "Secure" not in insecure
    assert cleared.startswith('xuanpu_session=""')
    assert "Max-Age=0" in cleared
    assert "Secure" in cleared


# --- 防跨站请求伪造 ---


async def test_cookie_writes_need_the_custom_header(ident: Ident) -> None:
    """cookie 认证的写请求缺 ``X-Requested-By: xuanpu``（或值不对）：403 csrf_rejected。"""
    admin = await ident.user(role="tenant_admin")
    token = await ident.login(admin)
    for extra in ({}, {"X-Requested-By": "other"}):
        response = await ident.http.put(
            "/v1/secrets/s1", headers={**cookie(token), **extra}, json={"value": "v"}
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "csrf_rejected"
    deleted = await ident.http.delete("/v1/secrets/s1", headers=cookie(token))
    assert deleted.json()["error"]["code"] == "csrf_rejected"
    # 被拒的写请求什么也没做：只有开通时用管理员 key 写的那一条
    assert [record.data["name"] for record in await ident.audit("secret_written")] == ["crm-token"]


@pytest.mark.parametrize(
    "origin", ["https://evil.example.com", "http://xuanpu.example.com", "null"]
)
async def test_cookie_writes_from_another_origin_are_rejected(ident: Ident, origin: str) -> None:
    """带了 ``Origin`` 而它不是 ``public_url`` 的源：403。"""
    admin = await ident.user(role="tenant_admin")
    response = await ident.http.put(
        "/v1/secrets/s1",
        headers={**cookie(await ident.login(admin)), **WRITE, "Origin": origin},
        json={"value": "v"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_rejected"


async def test_cookie_writes_from_the_platform_pass(ident: Ident) -> None:
    """带自定义头、``Origin`` 等于 ``public_url`` 的源（或不带 ``Origin``）：放行。"""
    admin = await ident.user(role="tenant_admin")
    token = await ident.login(admin)
    for extra in ({"Origin": PUBLIC_URL}, {}):
        response = await ident.http.put(
            "/v1/secrets/s1", headers={**cookie(token), **WRITE, **extra}, json={"value": "v"}
        )
        assert response.status_code == 200, response.text


async def test_reads_and_key_requests_are_not_restricted(ident: Ident) -> None:
    """GET 不受限；用 API key 的写请求不受限（浏览器不会自动带 ``Authorization``）。"""
    admin = await ident.user(role="tenant_admin")
    evil = {"Origin": "https://evil.example.com"}
    read = await ident.http.get("/v1/secrets", headers={**cookie(await ident.login(admin)), **evil})
    assert read.status_code == 200
    write = await ident.http.put(
        "/v1/secrets/s1", headers={**bearer(ident.world.admin_key), **evil}, json={"value": "v"}
    )
    assert write.status_code == 200


async def test_without_public_url_only_the_custom_header_is_checked(no_public_url: Ident) -> None:
    """没配 ``public_url``：没有可比的源，不查 ``Origin``；自定义头照样必须带。"""
    admin = await no_public_url.user(role="tenant_admin")
    token = await no_public_url.login(admin)
    evil = {"Origin": "https://evil.example.com"}
    passed = await no_public_url.http.put(
        "/v1/secrets/s1", headers={**cookie(token), **WRITE, **evil}, json={"value": "v"}
    )
    assert passed.status_code == 200
    rejected = await no_public_url.http.put(
        "/v1/secrets/s1", headers={**cookie(token), **evil}, json={"value": "v"}
    )
    assert rejected.json()["error"]["code"] == "csrf_rejected"


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "get"])
def test_safe_methods_are_not_checked(method: str) -> None:
    """不改状态的方法不做检查。"""
    check_cookie_request(method, {"Origin": "https://evil"}, public_origin=PUBLIC_URL)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_write_methods_are_checked(method: str) -> None:
    """写方法都要过检查。"""
    with pytest.raises(CsrfRejectedError):
        check_cookie_request(method, {}, public_origin=PUBLIC_URL)
    check_cookie_request(method, WRITE, public_origin=PUBLIC_URL)


# --- 系统代员工调用 ---


async def test_act_as_takes_the_lower_role(ident: Ident) -> None:
    """生效角色取 key 与员工中较低的。

    管理员 key 代成员只有成员权限，成员 key 代管理员也只有成员权限。
    """
    admin, member = await ident.user(role="tenant_admin"), await ident.user()
    admin_key, member_key = await ident.system_key(), await ident.system_key(role="tenant_member")
    cases = [
        (admin_key, admin, 200),
        (admin_key, member, 403),
        (member_key, admin, 403),
        (member_key, member, 403),
    ]
    for key, user, status in cases:
        # 列密钥要租户管理员
        response = await ident.http.get("/v1/secrets", headers=act_as(key, user))
        assert response.status_code == status, (key, user.role)


async def _plain_key(ident: Ident) -> tuple[dict[str, str], str]:
    """key 没有代员工调用的能力。"""
    user = await ident.user()
    return act_as(ident.world.admin_key, user), "没有代员工调用的能力"


async def _other_tenant(ident: Ident) -> tuple[dict[str, str], str]:
    """员工属于别的租户。"""
    await ident.world.platform.create_tenant(ROOT_PRINCIPAL, tenant_id="globex", name="Globex")
    user = await ident.user(tenant="globex")
    return act_as(await ident.system_key(), user), "本租户没有这名启用的员工"


async def _disabled(ident: Ident) -> tuple[dict[str, str], str]:
    """员工已停用。"""
    user = await ident.user()
    await ident.store.set_user_status(
        "acme", user.user_id, "disabled", reason="admin", at=time.time()
    )
    return act_as(await ident.system_key(), user), "本租户没有这名启用的员工"


async def _unknown_user(ident: Ident) -> tuple[dict[str, str], str]:
    """没有这名员工。"""
    headers = {**bearer(await ident.system_key()), "X-Xuanpu-Act-As": new_user_id()}
    return headers, "本租户没有这名启用的员工"


async def _root(ident: Ident) -> tuple[dict[str, str], str]:
    """根凭据不能代员工调用。"""
    return act_as(ident.world.root_key, await ident.user()), "根凭据"


async def _with_cookie(ident: Ident) -> tuple[dict[str, str], str]:
    """登录 cookie 不能带代调用的头。"""
    user = await ident.user()
    return {**cookie(await ident.login(user)), "X-Xuanpu-Act-As": user.user_id}, "只能用 API key"


@pytest.mark.parametrize(
    "case", [_plain_key, _other_tenant, _disabled, _unknown_user, _root, _with_cookie]
)
async def test_act_as_refusals(ident: Ident, case: Any) -> None:
    """代员工调用被拒的情形：403 permission_denied。"""
    headers, fragment = await case(ident)
    response = await ident.http.get("/v1/me", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    assert fragment in response.json()["error"]["message"]


async def test_act_as_with_a_bad_key_is_unauthenticated(ident: Ident) -> None:
    """先认 key 再看代调用：key 无效是 401。"""
    user = await ident.user()
    response = await ident.http.get("/v1/me", headers=act_as("xp_000000000000_nope", user))
    assert response.status_code == 401
    assert response.json() == UNAUTHENTICATED


# --- 签发带代调用能力的 key ---


async def test_issuing_act_as_keys(ident: Ident) -> None:
    """管理员（租户或平台）能签发带 ``act_as_users`` 的 key：响应与列表里有这一项，审计记下它。"""
    by_admin = await ident.http.post(
        "/v1/keys",
        headers=bearer(ident.world.admin_key),
        json={"name": "system", "act_as_users": True},
    )
    assert by_admin.status_code == 201
    assert by_admin.json()["act_as_users"] is True
    by_root = await ident.http.post(
        "/v1/admin/tenants/acme/keys",
        headers=bearer(ident.world.root_key),
        json={"name": "system-2", "role": "tenant_admin", "act_as_users": True},
    )
    assert by_root.json()["act_as_users"] is True
    plain = await ident.http.post(
        "/v1/keys", headers=bearer(ident.world.admin_key), json={"name": "plain"}
    )
    assert plain.json()["act_as_users"] is False

    listed = await ident.http.get("/v1/keys", headers=bearer(ident.world.admin_key))
    flags = {item["name"]: item["act_as_users"] for item in listed.json()["keys"]}
    assert flags == {"admin": False, "app": False, "system": True, "system-2": True, "plain": False}
    issued = {record.data["name"]: record.data for record in await ident.audit("api_key_issued")}
    assert issued["system"]["act_as_users"] is True
    assert issued["plain"]["act_as_users"] is False

    # 签发出来的 key 真能代员工调用
    member = await ident.user()
    me = await ident.http.get("/v1/me", headers=act_as(by_admin.json()["key"], member))
    assert me.json()["user_id"] == member.user_id


async def test_members_cannot_issue_act_as_keys(ident: Ident) -> None:
    """成员（API key、浏览器登录、被代调用的成员）签发带代调用能力的 key：403。"""
    member = await ident.user()
    body = {"name": "sneaky", "act_as_users": True}
    attempts = [
        bearer(ident.world.member_key),
        {**cookie(await ident.login(member)), **WRITE},
        act_as(await ident.system_key(), member),
    ]
    for headers in attempts:
        response = await ident.http.post("/v1/keys", headers=headers, json=body)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"
    assert [record.data["name"] for record in await ident.audit("api_key_issued")] == [
        "admin",
        "app",
        "system",
    ]
    with pytest.raises(PermissionDeniedError):
        await ident.world.platform.issue_key(
            ident.world.member, tenant_id=None, name="x", role="tenant_member", act_as_users=True
        )


# --- 装配 ---

_CONFIG: dict[str, Any] = {
    "root_key_hash": "a" * 64,
    "master_keys": {"k1": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
    "primary_master_key": "k1",
    "models": {
        "base_url": "http://gateway:4000/v1",
        "api_key": "sk-test",
        "tiers": {tier: {"model": "m"} for tier in ("fast", "balanced", "deep")},
    },
}


async def test_memory_bootstrap_wires_identity(tmp_path: Path) -> None:
    """内存控制面：身份存储以它为准；``public_url`` 规整后与 ``identity`` 段一起交到身份服务。"""
    document = {
        **_CONFIG,
        "data_dir": str(tmp_path / "data"),
        "public_url": "HTTPS://Xuanpu.Example.com:443/",
        "identity": {"cookie_secure": False, "session_ttl_seconds": 3600},
    }
    async with open_platform(parse_config(json.dumps(document), variables={})) as platform:
        assert isinstance(platform.identity.store, InMemoryIdentityStore)
        assert platform.identity.settings == IdentitySettings(
            public_url="https://xuanpu.example.com", cookie_secure=False, session_ttl_seconds=3600
        )
        await platform.create_tenant(ROOT_PRINCIPAL, tenant_id="acme", name="Acme")
        principal = await _login_through(platform)
        assert principal.actor.startswith("user:usr_")


async def test_postgres_bootstrap_shares_the_control_plane_pool(
    tmp_path: Path, postgres_dsn: str
) -> None:
    """Postgres 控制面：身份存储与控制面共用连接池与命名空间，登录会话能认出员工。"""
    schema = "t_" + secrets.token_hex(6)
    pool = await asyncpg.create_pool(postgres_dsn, min_size=1, max_size=2)
    try:
        await apply_migrations(pool, SqlNamespace(schema=schema, prefix="xp_"))
        document = {
            **_CONFIG,
            "data_dir": str(tmp_path / "data"),
            "control_plane": {
                "backend": "postgres",
                "postgres": {"dsn": "${PG}", "schema": schema},
            },
        }
        config = parse_config(json.dumps(document), variables={"PG": postgres_dsn})
        async with open_platform(config) as platform:
            assert isinstance(platform.identity.store, PostgresIdentityStore)
            await platform.create_tenant(ROOT_PRINCIPAL, tenant_id="acme", name="Acme")
            principal = await _login_through(platform)
            assert principal.tenant_id == "acme"
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await pool.close()


async def _login_through(platform: Any) -> Principal:
    """在租户 acme 里建一名员工与登录会话，再按 cookie 令牌认出他。"""
    user = UserRecord(
        tenant_id="acme", user_id=new_user_id(), display_name="王五", created_at=time.time()
    )
    await platform.identity.store.create_user(user)
    token, token_hash = new_login_token()
    now = time.time()
    await platform.identity.store.create_login_session(
        LoginSession(
            token_hash=token_hash,
            tenant_id="acme",
            user_id=user.user_id,
            created_at=now,
            expires_at=now + 60,
            last_seen_at=now,
        )
    )
    principal: Principal = await platform.identity.authenticate_login(token)
    assert principal.user_id == user.user_id
    return principal
