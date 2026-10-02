"""飞书事件回调：``POST /v1/identity/feishu/events/{tenant_id}``（设计 7.3，另有对齐表里的调整）。

样本按设计第 3 节的算法在这里独立造出来（用 ``cryptography``，不调平台的实现）：请求体
``{"encrypt": base64(IV + AES-256-CBC(PKCS7(明文)))}``，密钥是 Encrypt Key 的 SHA-256；签名是
``sha256(timestamp + nonce + Encrypt Key + 原始请求体)`` 的十六进制。

飞书发来的 URL 验证请求是加密的、**不带签名头**（官方 SDK 先处理 URL 验证再验签），所以没有签名的
请求只认能用 Encrypt Key 解开、Verification Token 对得上的 URL 验证；其余事件必须带对签名。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from prometheus_client.parser import text_string_to_metric_families
from xuanpu_server.controlplane.models import AuditQuery
from xuanpu_server.identity import ExternalIdentity, UserRecord, new_user_id
from xuanpu_server.identity import events as feishu_events
from xuanpu_server.identity.events import MAX_EVENT_BODY_BYTES, reason_from_status
from xuanpu_server.platform import ROOT_PRINCIPAL

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx
    from xuanpu_server.tenancy import TenantRole

APP_ID = "cli_test_app"
ENCRYPT_KEY = "feishu-encrypt-key-PLAINTEXT"
VERIFICATION_TOKEN = "feishu-verification-PLAINTEXT"
TENANT_KEY = "tk_acme"
"""与 ``conftest.py`` 里写进保管库、飞书配置的取值一致。"""
EVENTS = "/v1/identity/feishu/events/acme"
WRITE = {"X-Requested-By": "xuanpu"}


# --- 造样本 ---


def encrypt_raw(key: str, data: bytes, *, iv: bytes | None = None) -> str:
    """不加填充直接加密（``data`` 必须是 16 的倍数），用来造填充错误的样本。"""
    iv = iv if iv is not None else os.urandom(16)
    encryptor = Cipher(algorithms.AES(hashlib.sha256(key.encode()).digest()), modes.CBC(iv))
    sealer = encryptor.encryptor()
    return base64.b64encode(iv + sealer.update(data) + sealer.finalize()).decode()


def encrypt(key: str, plaintext: bytes) -> str:
    """按飞书的算法加密：PKCS7 填充、AES-256-CBC、IV 放在密文前面、base64。"""
    padder = padding.PKCS7(128).padder()
    return encrypt_raw(key, padder.update(plaintext) + padder.finalize())


def sealed(payload: Any, *, key: str = ENCRYPT_KEY) -> bytes:
    """加密后的请求体。"""
    plaintext = json.dumps(payload, ensure_ascii=False).encode()
    return json.dumps({"encrypt": encrypt(key, plaintext)}).encode()


def sign(timestamp: str, nonce: str, key: str, body: bytes) -> str:
    """飞书的签名。"""
    return hashlib.sha256((timestamp + nonce + key).encode() + body).hexdigest()


def signed_headers(body: bytes, *, key: str = ENCRYPT_KEY) -> dict[str, str]:
    """带签名的请求头。"""
    timestamp, nonce = str(int(time.time())), "nonce-" + os.urandom(4).hex()
    return {
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": sign(timestamp, nonce, key, body),
        "Content-Type": "application/json",
    }


def url_verification(token: str = VERIFICATION_TOKEN) -> dict[str, Any]:
    """URL 验证请求解密后的样子。"""
    return {"challenge": "ajls384kdjx98XX", "token": token, "type": "url_verification"}


def deleted(union_id: str, **overrides: Any) -> dict[str, Any]:
    """员工离职事件（2.0 版）。

    ``overrides`` 改 header 里的项，其中的 ``status`` 改 ``event.object.status``。
    """
    status = overrides.pop("status", {"is_resigned": True, "is_frozen": False})
    header = {
        "event_id": "evt_0001",
        "token": VERIFICATION_TOKEN,
        "create_time": "1790900000000",
        "event_type": "contact.user.deleted_v3",
        "tenant_key": TENANT_KEY,
        "app_id": APP_ID,
        **overrides,
    }
    obj = {"open_id": "ou_x", "union_id": union_id, "user_id": "u1", "status": status}
    return {"schema": "2.0", "header": header, "event": {"object": obj, "old_object": {}}}


# --- 夹具与把手 ---


@pytest.fixture
async def ident(identity_world: Any) -> AsyncIterator[Any]:
    """配了飞书（已绑定飞书企业 ``tk_acme``）的平台，样例业务包已发布。"""
    async with identity_world() as item:
        await item.world.publish()
        await item.configure_feishu()
        await item.store.bind_tenant_key("acme", "feishu", TENANT_KEY)
        yield item


async def employee(ident: Any, union_id: str, *, role: TenantRole = "tenant_member") -> UserRecord:
    """建一名有飞书身份的员工。"""
    user = UserRecord(
        tenant_id="acme",
        user_id=new_user_id(),
        display_name=union_id,
        created_at=time.time(),
        role=role,
    )
    identity = ExternalIdentity(
        tenant_id="acme", provider="feishu", subject=union_id, user_id=user.user_id
    )
    await ident.store.create_user(user, identity=identity)
    return user


async def post(
    ident: Any, body: bytes, *, signed: bool = True, path: str = EVENTS, **sign_kwargs: Any
) -> httpx.Response:
    """把一个请求体发到事件地址（不带任何平台凭据）。"""
    headers = signed_headers(body, **sign_kwargs) if signed else {}
    response: httpx.Response = await ident.http.post(path, content=body, headers=headers)
    return response


def results(ident: Any) -> dict[str, float]:
    """``xuanpu_feishu_events_total`` 各个 ``result`` 的计数。"""
    text = ident.world.platform.metrics.render().decode()
    return {
        sample.labels["result"]: sample.value
        for family in text_string_to_metric_families(text)
        if family.name == "xuanpu_feishu_events"
        for sample in family.samples
    }


async def audit_count(ident: Any) -> int:
    """租户 acme 的审计记录总数。"""
    found = await ident.world.platform.store.query_audit("acme", AuditQuery(limit=1000))
    return len(found)


async def status_of(ident: Any, user: UserRecord) -> str:
    """员工当前的状态。"""
    current = await ident.store.get_user("acme", user.user_id)
    status: str = current.status
    return status


def unauthenticated(response: httpx.Response) -> bool:
    """是不是统一的 401。"""
    return response.status_code == 401 and response.json() == {
        "error": {"code": "unauthenticated", "message": "凭据无效"}
    }


# --- 纯计算 ---


def test_decrypt_matches_the_feishu_sample() -> None:
    """飞书文档给的样例：Encrypt Key ``test key`` 加密 ``hello world``。"""
    sample = "P37w+VZImNgPEO1RBhJ6RtKl7n6zymIbEG1pReEzghk="
    assert feishu_events.decrypt("test key", sample) == b"hello world"
    assert feishu_events.decrypt(ENCRYPT_KEY, encrypt(ENCRYPT_KEY, b"{}")) == b"{}"


def test_signature_matches_the_algorithm() -> None:
    """签名：``sha256(timestamp + nonce + Encrypt Key + 原始请求体)`` 的十六进制，常数时间比较。"""
    body = b'{"encrypt": "abc"}'
    expected = sign("1700000000", "n1", ENCRYPT_KEY, body)
    assert feishu_events.expected_signature("1700000000", "n1", ENCRYPT_KEY, body) == expected
    assert feishu_events.signature_matches(
        expected, timestamp="1700000000", nonce="n1", encrypt_key=ENCRYPT_KEY, body=body
    )
    assert not feishu_events.signature_matches(
        expected, timestamp="1700000001", nonce="n1", encrypt_key=ENCRYPT_KEY, body=body
    )
    assert not feishu_events.signature_matches(
        "签名", timestamp="1700000000", nonce="n1", encrypt_key=ENCRYPT_KEY, body=body
    )


@pytest.mark.parametrize(
    ("flags", "reason"),
    [
        ({"is_resigned": True}, "feishu_resigned"),
        ({"is_frozen": True}, "feishu_frozen"),
        ({"is_exited": True}, "feishu_exited"),
        ({"is_resigned": True, "is_frozen": True, "is_exited": True}, "feishu_resigned"),
        ({"is_frozen": True, "is_exited": True}, "feishu_exited"),
        ({"is_resigned": False, "is_frozen": None}, None),
        ({}, None),
    ],
)
def test_reason_from_status(flags: dict[str, bool | None], reason: str | None) -> None:
    """停用原因：离职 > 主动退出 > 暂停；都没有为 None（离职事件里另按离职处理）。"""
    assert reason_from_status(**flags) == reason


# --- URL 验证 ---


async def test_url_verification_answers_the_challenge(ident: Any) -> None:
    """加密的 URL 验证（飞书发的不带签名头；带了也行）：Verification Token 对得上就原样回
    challenge，    在 1 秒内。"""
    body = sealed(url_verification())
    for signed in (False, True):
        started = time.monotonic()
        response = await post(ident, body, signed=signed)
        assert time.monotonic() - started < 1.0
        assert (response.status_code, response.json()) == (200, {"challenge": "ajls384kdjx98XX"})
    assert results(ident)["url_verification"] == 2.0


async def test_url_verification_with_wrong_token_is_refused(ident: Any) -> None:
    """Verification Token 不对：401，不回 challenge。"""
    for signed in (False, True):
        response = await post(ident, sealed(url_verification(token="wrong")), signed=signed)
        assert unauthenticated(response)
        assert "challenge" not in response.text


async def test_plain_url_verification_is_refused(ident: Any, caplog: Any) -> None:
    """不加密的 URL 验证（飞书后台没配 Encrypt Key 时才会这样发）：平台要求加密，401，不回
    challenge；日志里提示去配 Encrypt Key——否则验证通过了，之后的事件却全都验不了签。"""
    body = json.dumps(url_verification()).encode()
    with caplog.at_level(logging.WARNING, logger="xuanpu_server.identity.callback"):
        response = await post(ident, body, signed=False)
    assert unauthenticated(response)
    assert "Encrypt Key" in caplog.text


# --- 签名与来源 ---


async def test_bad_signature_is_401_without_audit(ident: Any) -> None:
    """签名不符（Encrypt Key 不对、请求体被改、缺签名用的头）：401，员工不动，**不写审计**，
    计数。"""
    alice = await employee(ident, "on_alice")
    body = sealed(deleted("on_alice"))
    before = await audit_count(ident)

    wrong_key = await post(ident, body, key="another-key")
    headers = signed_headers(body)
    tampered = await ident.http.post(EVENTS, content=body + b" ", headers=headers)
    headers.pop("X-Lark-Request-Nonce")
    no_nonce = await ident.http.post(EVENTS, content=body, headers=headers)

    assert all(unauthenticated(item) for item in (wrong_key, tampered, no_nonce))
    assert await status_of(ident, alice) == "active"
    assert await audit_count(ident) == before
    assert results(ident)["bad_signature"] == 3.0


async def test_unsigned_event_is_refused(ident: Any) -> None:
    """没有签名的只认 URL 验证：加密的离职事件不带签名是 401，员工不动。"""
    alice = await employee(ident, "on_alice")
    response = await post(ident, sealed(deleted("on_alice")), signed=False)
    assert unauthenticated(response)
    assert await status_of(ident, alice) == "active"


async def test_unsigned_failures_look_the_same(ident: Any) -> None:
    """没有签名的请求，不论哪一步没过（base64、长度、填充、JSON、不是 URL 验证、Token 不对），响应
    逐字节相同：不给能拿来逐字节猜明文的填充预言机。"""
    bodies = [
        b"not json",
        json.dumps({"encrypt": "%%%"}).encode(),
        json.dumps({"encrypt": base64.b64encode(os.urandom(16 + 20)).decode()}).encode(),
        json.dumps({"encrypt": encrypt_raw(ENCRYPT_KEY, b"x" * 15 + b"\x00")}).encode(),
        json.dumps({"encrypt": encrypt(ENCRYPT_KEY, b"not json")}).encode(),
        sealed(deleted("on_alice")),
        sealed(url_verification(token="wrong")),
    ]
    responses = [await post(ident, body, signed=False) for body in bodies]
    assert {(item.status_code, item.content) for item in responses} == {
        (responses[0].status_code, responses[0].content)
    }
    assert unauthenticated(responses[0])


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"token": "wrong"}, id="token"),
        pytest.param({"app_id": "cli_other"}, id="app_id"),
        pytest.param({"tenant_key": "tk_other"}, id="tenant_key"),
    ],
)
async def test_mismatched_source_is_refused(ident: Any, override: dict[str, str]) -> None:
    """签名对了，但 ``header`` 里的 Verification Token、App ID 或飞书企业对不上：401，员工不动。"""
    alice = await employee(ident, "on_alice")
    before = await audit_count(ident)
    response = await post(ident, sealed(deleted("on_alice", **override)))
    assert unauthenticated(response)
    assert await status_of(ident, alice) == "active"
    assert await audit_count(ident) == before
    assert results(ident)["rejected"] == 1.0


async def test_unbound_tenant_key_is_not_trusted(identity_world: Any, caplog: Any) -> None:
    """租户还没绑定飞书企业（还没有员工登录过）：核对不了 ``tenant_key``，离职事件不处理——200 忽略并
    记日志，也不拿事件去绑定（绑定只能由真实的登录建立）。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        alice = await employee(ident, "on_alice")
        with caplog.at_level(logging.INFO, logger="xuanpu_server.identity.callback"):
            response = await post(ident, sealed(deleted("on_alice")))
        assert response.status_code == 200
        assert await status_of(ident, alice) == "active"
        config = await ident.store.get_identity_config("acme", "feishu")
        assert config.feishu_tenant_key is None
        assert results(ident)["ignored"] == 1.0
        assert "绑定" in caplog.text


# --- 离职事件 ---


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ({"is_resigned": True, "is_frozen": False}, "feishu_resigned"),
        ({"is_frozen": True}, "feishu_frozen"),
        ({"is_exited": True}, "feishu_exited"),
        ({}, "feishu_resigned"),
    ],
)
async def test_resignation_disables_the_employee(
    ident: Any, status: dict[str, bool], reason: str
) -> None:
    """离职事件：同步标停用、删登录会话（cookie 立即 401），关会话放后台（等它跑完后会话已关）；
    ``user_disabled`` 的 ``actor`` 是 ``system:feishu-event``，带原因与事件标识。"""
    alice = await employee(ident, "on_alice")
    cookie = {**ident.cookie(await ident.login(alice)), **WRITE}
    created = await ident.http.post(
        "/v1/sessions", headers=cookie, json={"package": "support-desk"}
    )
    session_id = created.json()["session_id"]

    response = await post(ident, sealed(deleted("on_alice", status=status)))

    assert response.status_code == 200
    user = await ident.store.get_user("acme", alice.user_id)
    assert (user.status, user.disabled_reason) == ("disabled", reason)
    assert (await ident.http.get("/v1/me", headers=cookie)).status_code == 401
    await ident.world.platform.identity.disabling.settle()
    record = await ident.world.platform.store.get_session("acme", session_id)
    assert record.status == "closed"
    [audit] = await ident.audit("user_disabled")
    assert (audit.actor, audit.user_id) == ("system:feishu-event", alice.user_id)
    assert audit.data == {"reason": reason, "event_id": "evt_0001"}
    assert results(ident)["accepted"] == 1.0


async def test_resigned_last_admin_is_disabled_too(ident: Any) -> None:
    """离职的管理员即使是最后一名也照样停用（兜底靠平台管理员重新指定，见运维手册）。"""
    boss = await employee(ident, "on_boss", role="tenant_admin")
    response = await post(ident, sealed(deleted("on_boss")))
    assert response.status_code == 200
    assert await status_of(ident, boss) == "disabled"


async def test_redelivery_is_harmless(ident: Any) -> None:
    """飞书重推同一个事件：照样 200，状态不变、不再记 ``user_disabled``，监听者重跑。"""
    alice = await employee(ident, "on_alice")
    body = sealed(deleted("on_alice"))
    first, second = await post(ident, body), await post(ident, body)
    await ident.world.platform.identity.disabling.settle()
    assert (first.status_code, second.status_code) == (200, 200)
    assert len(await ident.audit("user_disabled")) == 1
    assert await status_of(ident, alice) == "disabled"
    assert results(ident)["accepted"] == 2.0


async def test_response_does_not_wait_for_the_listeners(ident: Any) -> None:
    """飞书要 3 秒内回 200：监听者在后台跑，响应不等它们。"""
    gate = asyncio.Event()

    class Slow:
        """等测试放行才结束的监听者。"""

        async def on_user_disabled(self, tenant_id: str, user_id: str) -> None:
            """等放行。"""
            await gate.wait()

    pipeline = ident.world.platform.identity.disabling
    pipeline.register("slow", Slow())
    await employee(ident, "on_alice")
    started = time.monotonic()
    response = await post(ident, sealed(deleted("on_alice")))
    assert response.status_code == 200
    assert time.monotonic() - started < 3.0
    assert pipeline.pending == 1
    gate.set()
    await pipeline.settle()
    assert pipeline.pending == 0


async def test_unknown_employee_and_other_events_are_ignored(ident: Any) -> None:
    """找不到这名员工、别的事件类型、1.0 版事件：200，什么也不做。"""
    alice = await employee(ident, "on_alice")
    other_type = deleted("on_alice", event_type="contact.user.updated_v3")
    v1 = {
        "uuid": "u",
        "token": VERIFICATION_TOKEN,
        "ts": "1",
        "type": "event_callback",
        "event": {},
    }
    for payload in (deleted("on_nobody"), other_type, v1):
        response = await post(ident, sealed(payload))
        assert (response.status_code, response.json()) == (200, {"msg": "success"})
    assert await status_of(ident, alice) == "active"
    assert results(ident)["ignored"] == 3.0
    assert await ident.audit("user_disabled") == []


# --- 坏请求 ---


def malformed_bodies() -> list[Any]:
    """签名对、内容不对的请求体：一律 400，不是 500。"""
    good_union = deleted("on_alice")
    no_union = deleted("on_alice")
    del no_union["event"]["object"]["union_id"]
    bad_status = deleted("on_alice", status={"is_resigned": "yes"})
    cases = {
        "outer_not_json": b"not json",
        "outer_not_object": b"[1, 2]",
        "no_encrypt": json.dumps({"challenge": "x"}).encode(),
        "encrypt_not_string": json.dumps({"encrypt": 42}).encode(),
        "bad_base64": json.dumps({"encrypt": "!!!not-base64!!!"}).encode(),
        "not_block_aligned": json.dumps(
            {"encrypt": base64.b64encode(os.urandom(16 + 20)).decode()}
        ).encode(),
        "too_short": json.dumps({"encrypt": base64.b64encode(os.urandom(16)).decode()}).encode(),
        "bad_padding": json.dumps(
            {"encrypt": encrypt_raw(ENCRYPT_KEY, b"x" * 15 + b"\x00")}
        ).encode(),
        "wrong_key": sealed(good_union, key="another-key"),
        "plaintext_not_json": json.dumps({"encrypt": encrypt(ENCRYPT_KEY, b"\xff\xfe")}).encode(),
        "plaintext_not_object": json.dumps({"encrypt": encrypt(ENCRYPT_KEY, b"[]")}).encode(),
        "deep_nesting": json.dumps({"encrypt": encrypt(ENCRYPT_KEY, b"[" * 100_000)}).encode(),
        "no_header": sealed({"schema": "2.0", "event": {}}),
        "no_union_id": sealed(no_union),
        "status_not_bool": sealed(bad_status),
    }
    return [pytest.param(body, id=name) for name, body in cases.items()]


@pytest.mark.parametrize("body", malformed_bodies())
async def test_malformed_signed_bodies_are_400(ident: Any, body: bytes) -> None:
    """签名对、内容不合预期（含填充错误、长度不是 16 的倍数、base64 非法）：400 而不是 500，
    员工不动。"""
    alice = await employee(ident, "on_alice")
    response = await post(ident, body)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"
    assert await status_of(ident, alice) == "active"
    assert results(ident)["bad_request"] == 1.0


async def test_oversized_body_is_refused(ident: Any) -> None:
    """请求体超过 1 MiB：400，不读完、不验签。"""
    body = b"x" * (MAX_EVENT_BODY_BYTES + 1)
    response = await post(ident, body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert results(ident)["bad_request"] == 1.0


async def test_unknown_tenant_is_404(ident: Any) -> None:
    """租户不存在、标识不合法、没有配置飞书：404。"""
    await ident.world.platform.create_tenant(ROOT_PRINCIPAL, tenant_id="beta", name="Beta")
    body = sealed(url_verification())
    for tenant in ("nobody", "Bad_ID", "beta"):
        response = await post(
            ident, body, signed=False, path=f"/v1/identity/feishu/events/{tenant}"
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    assert results(ident)["unknown_tenant"] == 3.0


async def test_missing_secret_is_404(ident: Any, caplog: Any) -> None:
    """飞书配置引用的 Encrypt Key 被从保管库删掉了：验不了签，按没有可用配置处理（404），
    日志告警。"""
    await ident.world.platform.delete_secret(ident.world.admin, "feishu-encrypt-key")
    with caplog.at_level(logging.WARNING, logger="xuanpu_server.identity.callback"):
        response = await post(ident, sealed(url_verification()), signed=False)
    assert response.status_code == 404
    assert "feishu-encrypt-key" in caplog.text
