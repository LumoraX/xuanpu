"""飞书事件回调：``POST /v1/identity/feishu/events/{tenant_id}``（设计 7.3，另有对齐表里的调整）。

本文件是正常的流程：纯计算、URL 验证、签名与来源、离职事件；各种坏请求在
``test_feishu_event_errors.py``。样本与把手见本目录的 ``conftest.py``。

飞书发来的 URL 验证请求是加密的、**不带签名头**（官方 SDK 先处理 URL 验证再验签），所以没有签名的
请求只认能用 Encrypt Key 解开、Verification Token 对得上的 URL 验证；其余事件必须带对签名。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import pytest
from xuanpu_server.identity import events as feishu_events
from xuanpu_server.identity.events import EventFormatError, reason_from_status

if TYPE_CHECKING:
    import httpx

EVENTS = "/v1/identity/feishu/events/acme"
WRITE = {"X-Requested-By": "xuanpu"}


class Recorder:
    """记下每次被通知的监听者。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def on_user_disabled(self, tenant_id: str, user_id: str) -> None:
        """记一笔。"""
        self.calls.append((tenant_id, user_id))


# --- 纯计算 ---


def test_decrypt_matches_the_feishu_sample(samples: Any) -> None:
    """飞书文档给的样例：Encrypt Key ``test key`` 加密 ``hello world``。"""
    sample = "P37w+VZImNgPEO1RBhJ6RtKl7n6zymIbEG1pReEzghk="
    assert feishu_events.decrypt("test key", sample) == b"hello world"
    key = samples.encrypt_key
    assert feishu_events.decrypt(key, samples.encrypt(key, b"{}")) == b"{}"


def test_decrypt_insists_on_strict_pkcs7(samples: Any) -> None:
    """填充字节不一致的密文：只看最后一个字节去填充，解出来恰好是合法的 URL 验证；严格的 PKCS7
    拒绝。"""
    encrypted = json.loads(samples.loosely_padded(samples.url_verification()))["encrypt"]
    with pytest.raises(EventFormatError, match="填充"):
        feishu_events.decrypt(samples.encrypt_key, encrypted)
    # 样本有意义：宽松去填充解出来是合法的 URL 验证
    assert json.loads(samples.lenient_decrypt(encrypted)) == samples.url_verification()


def test_signature_matches_the_algorithm(samples: Any) -> None:
    """签名：``sha256(timestamp + nonce + Encrypt Key + 原始请求体)`` 的十六进制，常数时间比较。"""
    body, key = b'{"encrypt": "abc"}', samples.encrypt_key
    expected = samples.sign("1700000000", "n1", key, body)
    assert feishu_events.expected_signature("1700000000", "n1", key, body) == expected
    check = {"nonce": "n1", "encrypt_key": key, "body": body}
    assert feishu_events.signature_matches(expected, timestamp="1700000000", **check)
    assert not feishu_events.signature_matches(expected, timestamp="1700000001", **check)
    assert not feishu_events.signature_matches("签名", timestamp="1700000000", **check)


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


async def test_url_verification_answers_the_challenge(callback: Any) -> None:
    """加密的 URL 验证（飞书发的不带签名头；带了也行）：Verification Token 对得上就原样回
    challenge，在 1 秒内。"""
    body = callback.samples.sealed(callback.samples.url_verification())
    for signed in (False, True):
        started = time.monotonic()
        response = await callback.post(body, signed=signed)
        assert time.monotonic() - started < 1.0
        assert (response.status_code, response.json()) == (200, {"challenge": "ajls384kdjx98XX"})
    assert callback.results()["url_verification"] == 2.0


async def test_url_verification_with_wrong_token_is_refused(callback: Any) -> None:
    """Verification Token 不对：401，不回 challenge。"""
    samples = callback.samples
    for signed in (False, True):
        body = samples.sealed(samples.url_verification(token="wrong"))
        response = await callback.post(body, signed=signed)
        assert callback.unauthenticated(response)
        assert "challenge" not in response.text


async def test_plain_url_verification_is_refused(callback: Any, caplog: Any) -> None:
    """不加密的 URL 验证（飞书后台没配 Encrypt Key 时才会这样发）：平台要求加密，401，不回
    challenge；日志里提示去配 Encrypt Key——否则验证通过了，之后的事件却全都验不了签。"""
    body = json.dumps(callback.samples.url_verification()).encode()
    with caplog.at_level(logging.WARNING, logger="xuanpu_server.identity.callback"):
        response = await callback.post(body, signed=False)
    assert callback.unauthenticated(response)
    assert "Encrypt Key" in caplog.text


# --- 签名与来源 ---


async def test_bad_signature_is_401_without_audit(callback: Any) -> None:
    """签名不符（Encrypt Key 不对、请求体被改、缺签名用的头）：401，员工不动，**不写审计**，
    计数。"""
    samples, http = callback.samples, callback.ident.http
    alice = await callback.employee("on_alice")
    body = samples.sealed(samples.deleted("on_alice"))
    before = await callback.audit_count()

    wrong_key = await callback.post(body, key="another-key")
    headers = samples.signed_headers(body)
    tampered = await http.post(EVENTS, content=body + b" ", headers=headers)
    headers.pop("X-Lark-Request-Nonce")
    no_nonce = await http.post(EVENTS, content=body, headers=headers)

    assert all(callback.unauthenticated(item) for item in (wrong_key, tampered, no_nonce))
    assert await callback.status_of(alice) == "active"
    assert await callback.audit_count() == before
    assert callback.results()["bad_signature"] == 3.0


async def test_unsigned_event_is_refused(callback: Any) -> None:
    """没有签名的只认 URL 验证：加密的离职事件不带签名是 401，员工不动。"""
    alice = await callback.employee("on_alice")
    body = callback.samples.sealed(callback.samples.deleted("on_alice"))
    response = await callback.post(body, signed=False)
    assert callback.unauthenticated(response)
    assert await callback.status_of(alice) == "active"


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"token": "wrong"}, id="token"),
        pytest.param({"app_id": "cli_other"}, id="app_id"),
        pytest.param({"tenant_key": "tk_other"}, id="tenant_key"),
    ],
)
async def test_mismatched_source_is_refused(callback: Any, override: dict[str, str]) -> None:
    """签名对了，但 ``header`` 里的 Verification Token、App ID 或飞书企业对不上：401，员工不动。"""
    alice = await callback.employee("on_alice")
    before = await callback.audit_count()
    body = callback.samples.sealed(callback.samples.deleted("on_alice", **override))
    response = await callback.post(body)
    assert callback.unauthenticated(response)
    assert await callback.status_of(alice) == "active"
    assert await callback.audit_count() == before
    assert callback.results()["rejected"] == 1.0


async def test_unbound_tenant_key_is_not_trusted(unbound_callback: Any, caplog: Any) -> None:
    """租户还没绑定飞书企业（还没有员工登录过）：核对不了 ``tenant_key``，离职事件不处理——200 忽略
    并记日志，也不拿事件去绑定（绑定只能由真实的登录建立）。"""
    async with unbound_callback() as unbound:
        alice = await unbound.employee("on_alice")
        with caplog.at_level(logging.INFO, logger="xuanpu_server.identity.callback"):
            response = await unbound.post(
                unbound.samples.sealed(unbound.samples.deleted("on_alice"))
            )
        assert response.status_code == 200
        assert await unbound.status_of(alice) == "active"
        config = await unbound.ident.store.get_identity_config("acme", "feishu")
        assert config.feishu_tenant_key is None
        assert unbound.results()["ignored"] == 1.0
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
    callback: Any, status: dict[str, bool], reason: str
) -> None:
    """离职事件：同步标停用、删登录会话（cookie 立即 401），关会话放后台（等它跑完后会话已关）；
    ``user_disabled`` 的 ``actor`` 是 ``system:feishu-event``，带原因与事件标识。"""
    ident = callback.ident
    alice = await callback.employee("on_alice")
    cookie = {**ident.cookie(await ident.login(alice)), **WRITE}
    created = await ident.http.post(
        "/v1/sessions", headers=cookie, json={"package": "support-desk"}
    )
    session_id = created.json()["session_id"]

    body = callback.samples.sealed(callback.samples.deleted("on_alice", status=status))
    response = await callback.post(body)

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
    assert callback.results()["accepted"] == 1.0


async def test_resigned_last_admin_is_disabled_too(callback: Any) -> None:
    """离职的管理员即使是最后一名也照样停用（兜底靠平台管理员重新指定，见运维手册）。"""
    boss = await callback.employee("on_boss", role="tenant_admin")
    response = await callback.post(callback.samples.sealed(callback.samples.deleted("on_boss")))
    assert response.status_code == 200
    assert await callback.status_of(boss) == "disabled"


async def test_redelivery_is_harmless(callback: Any) -> None:
    """飞书重推同一个事件：照样 200，状态不变、不再记 ``user_disabled``，监听者确实重跑了。"""
    recorder = Recorder()
    pipeline = callback.ident.world.platform.identity.disabling
    pipeline.register("recorder", recorder)
    alice = await callback.employee("on_alice")
    body = callback.samples.sealed(callback.samples.deleted("on_alice"))

    first = await callback.post(body)
    await pipeline.settle()
    second = await callback.post(body)
    await pipeline.settle()

    assert (first.status_code, second.status_code) == (200, 200)
    assert len(await callback.ident.audit("user_disabled")) == 1
    assert await callback.status_of(alice) == "disabled"
    assert recorder.calls == [("acme", alice.user_id)] * 2
    assert callback.results()["accepted"] == 2.0


async def test_response_does_not_wait_for_the_listeners(callback: Any) -> None:
    """飞书要 3 秒内回 200：监听者在后台跑，响应不等它们。"""
    gate = asyncio.Event()

    class Slow:
        """等测试放行才结束的监听者。"""

        async def on_user_disabled(self, tenant_id: str, user_id: str) -> None:
            """等放行。"""
            await gate.wait()

    pipeline = callback.ident.world.platform.identity.disabling
    pipeline.register("slow", Slow())
    await callback.employee("on_alice")
    started = time.monotonic()
    response = await callback.post(callback.samples.sealed(callback.samples.deleted("on_alice")))
    assert response.status_code == 200
    assert time.monotonic() - started < 3.0
    assert pipeline.pending == 1
    gate.set()
    await pipeline.settle()
    assert pipeline.pending == 0


async def test_unknown_employee_and_other_events_are_ignored(callback: Any) -> None:
    """找不到这名员工、别的事件类型、1.0 版事件：200，什么也不做。"""
    samples = callback.samples
    alice = await callback.employee("on_alice")
    other_type = samples.deleted("on_alice", event_type="contact.user.updated_v3")
    v1 = {"uuid": "u", "token": samples.verification_token, "ts": "1", "type": "event_callback"}
    for payload in (samples.deleted("on_nobody"), other_type, v1):
        response: httpx.Response = await callback.post(samples.sealed(payload))
        assert (response.status_code, response.json()) == (200, {"msg": "success"})
    assert await callback.status_of(alice) == "active"
    assert callback.results()["ignored"] == 3.0
    assert await callback.ident.audit("user_disabled") == []
