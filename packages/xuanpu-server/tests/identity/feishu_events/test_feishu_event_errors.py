"""飞书事件回调的坏请求：没有签名的一律同一个 401；签名对、内容不对的 400；请求体过大；没有可用
配置的租户；平台内部出错。样本与把手见本目录的 ``conftest.py``。

没有签名的请求谁都能发，不论哪一步没过（含孤立代理项这类在别处才会出错的），响应都要逐字节相同：
不给能逐字节猜明文的填充预言机，也不回显内部错误。
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import TYPE_CHECKING, Any

import pytest
from xuanpu_server.identity.events import MAX_CHALLENGE_LENGTH, MAX_EVENT_BODY_BYTES
from xuanpu_server.platform import ROOT_PRINCIPAL
from xuanpu_server.secrets import SecretCorruptedError

if TYPE_CHECKING:
    from collections.abc import Callable

LONE_SURROGATE = "\ud800"
"""JSON 里写成 ``"\\ud800"`` 的孤立代理项：解出来的字符串编码不成 UTF-8。"""
LONG_CHALLENGE = "c" * (MAX_CHALLENGE_LENGTH + 1)


def _random_bytes_b64(size: int) -> bytes:
    """``encrypt`` 是一段随机字节的 base64（长度由调用方定）。"""
    return json.dumps({"encrypt": base64.b64encode(os.urandom(size)).decode()}).encode()


def _encrypted(plaintext: bytes) -> Callable[[Any], bytes]:
    """``encrypt`` 是按飞书算法加密的这段明文（明文可以不是 JSON）。"""
    return lambda s: json.dumps({"encrypt": s.encrypt(s.encrypt_key, plaintext)}).encode()


def _bad_padding(s: Any) -> bytes:
    """最后一个字节是 0：不合 PKCS7。"""
    return json.dumps({"encrypt": s.encrypt_raw(s.encrypt_key, b"x" * 15 + b"\x00")}).encode()


def _without_union_id(s: Any) -> bytes:
    """离职事件缺 ``union_id``。"""
    payload = s.deleted("on_alice")
    del payload["event"]["object"]["union_id"]
    return bytes(s.sealed(payload))


UNSIGNED: dict[str, Callable[[Any], bytes]] = {
    "outer_not_json": lambda s: b"not json",
    "bad_base64": lambda s: json.dumps({"encrypt": "%%%"}).encode(),
    "not_block_aligned": lambda s: _random_bytes_b64(16 + 20),
    "bad_padding": _bad_padding,
    "loose_padding": lambda s: s.loosely_padded(s.url_verification()),
    "plaintext_not_json": _encrypted(b"not json"),
    "not_url_verification": lambda s: s.sealed(s.deleted("on_alice")),
    "wrong_token": lambda s: s.sealed(s.url_verification(token="wrong")),
    "surrogate_token": lambda s: s.sealed(s.url_verification(token=LONE_SURROGATE)),
    "surrogate_challenge": lambda s: s.sealed(s.url_verification(challenge=LONE_SURROGATE)),
    "long_challenge": lambda s: s.sealed(s.url_verification(challenge=LONG_CHALLENGE)),
}
"""没有签名、哪一步都过不了的请求体：一律同一个 401。"""

MALFORMED: dict[str, Callable[[Any], bytes]] = {
    "outer_not_json": lambda s: b"not json",
    "outer_not_object": lambda s: b"[1, 2]",
    "no_encrypt": lambda s: json.dumps({"challenge": "x"}).encode(),
    "encrypt_not_string": lambda s: json.dumps({"encrypt": 42}).encode(),
    "bad_base64": lambda s: json.dumps({"encrypt": "!!!not-base64!!!"}).encode(),
    "not_block_aligned": lambda s: _random_bytes_b64(16 + 20),
    "too_short": lambda s: _random_bytes_b64(16),
    "bad_padding": _bad_padding,
    "loose_padding": lambda s: s.loosely_padded(s.url_verification()),
    "wrong_key": lambda s: s.sealed(s.deleted("on_alice"), key="another-key"),
    "plaintext_not_json": _encrypted(b"\xff\xfe"),
    "plaintext_not_object": _encrypted(b"[]"),
    "deep_nesting": _encrypted(b"[" * 100_000),
    "no_header": lambda s: s.sealed({"schema": "2.0", "event": {}}),
    "no_union_id": _without_union_id,
    "status_not_bool": lambda s: s.sealed(s.deleted("on_alice", status={"is_resigned": "yes"})),
    "surrogate_token": lambda s: s.sealed(s.url_verification(token=LONE_SURROGATE)),
    "surrogate_challenge": lambda s: s.sealed(s.url_verification(challenge=LONE_SURROGATE)),
    "long_challenge": lambda s: s.sealed(s.url_verification(challenge=LONG_CHALLENGE)),
    "surrogate_header_token": lambda s: s.sealed(s.deleted("on_alice", token=LONE_SURROGATE)),
    "surrogate_event_id": lambda s: s.sealed(s.deleted("on_alice", event_id=LONE_SURROGATE)),
    "surrogate_union_id": lambda s: s.sealed(s.deleted(LONE_SURROGATE)),
}
"""签名对、内容不对的请求体：一律 400，不是 500，也不回显内部错误。"""


async def test_unsigned_failures_look_the_same(callback: Any) -> None:
    """没有签名的请求，不论哪一步没过（含孤立代理项、超长 challenge、严格 PKCS7 才拒的填充），响应
    逐字节相同，计 ``bad_signature``。"""
    responses = [
        await callback.post(build(callback.samples), signed=False) for build in UNSIGNED.values()
    ]
    assert {(item.status_code, item.content) for item in responses} == {
        (responses[0].status_code, responses[0].content)
    }
    assert callback.unauthenticated(responses[0])
    assert callback.results()["bad_signature"] == float(len(UNSIGNED))


@pytest.mark.parametrize("case", list(MALFORMED))
async def test_malformed_signed_bodies_are_400(callback: Any, case: str) -> None:
    """签名对、内容不合预期（含填充错误、长度不是 16 的倍数、base64 非法、孤立代理项）：400 而不是
    500，不回显编码错误，员工不动。"""
    alice = await callback.employee("on_alice")
    response = await callback.post(MALFORMED[case](callback.samples))
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["message"].startswith("事件请求不合预期：")
    assert "codec" not in error["message"]
    assert await callback.status_of(alice) == "active"
    assert callback.results()["bad_request"] == 1.0


async def test_oversized_body_is_refused(callback: Any) -> None:
    """请求体超过 1 MiB：400，不读完、不验签。"""
    response = await callback.post(b"x" * (MAX_EVENT_BODY_BYTES + 1))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert callback.results()["bad_request"] == 1.0


async def test_unknown_tenant_is_404(callback: Any) -> None:
    """租户不存在、标识不合法、没有配置飞书：404。"""
    await callback.ident.world.platform.create_tenant(ROOT_PRINCIPAL, tenant_id="beta", name="Beta")
    body = callback.samples.sealed(callback.samples.url_verification())
    for tenant in ("nobody", "Bad_ID", "beta"):
        path = f"/v1/identity/feishu/events/{tenant}"
        response = await callback.post(body, signed=False, path=path)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    assert callback.results()["unknown_tenant"] == 3.0


async def test_missing_secret_is_404(callback: Any, caplog: Any) -> None:
    """飞书配置引用的 Encrypt Key 被从保管库删掉了：验不了签，按没有可用配置处理（404），
    日志告警。"""
    world = callback.ident.world
    await world.platform.delete_secret(world.admin, "feishu-encrypt-key")
    with caplog.at_level(logging.WARNING, logger="xuanpu_server.identity.callback"):
        body = callback.samples.sealed(callback.samples.url_verification())
        response = await callback.post(body, signed=False)
    assert response.status_code == 404
    assert "feishu-encrypt-key" in caplog.text


async def test_internal_errors_are_counted(callback: Any, monkeypatch: Any) -> None:
    """存储报错、保管库里的密文解不开：原样上抛（接入层回 500），计 ``internal_error``。"""
    samples = callback.samples
    handler = callback.ident.world.platform.identity.events
    await callback.employee("on_alice")
    body = samples.sealed(samples.deleted("on_alice"))
    headers = samples.signed_headers(body)
    signed = {
        "body": body,
        "signature": headers["X-Lark-Signature"],
        "timestamp": headers["X-Lark-Request-Timestamp"],
        "nonce": headers["X-Lark-Request-Nonce"],
    }

    async def store_down(*_: Any, **__: Any) -> None:
        """库挂了。"""
        raise ConnectionError("库挂了")

    monkeypatch.setattr(callback.ident.store, "get_user_by_identity", store_down)
    with pytest.raises(ConnectionError):
        await handler.handle("acme", **signed)

    async def corrupted(*_: Any, **__: Any) -> str:
        """密文解不开。"""
        raise SecretCorruptedError("解不开")

    monkeypatch.setattr(callback.ident.world.platform.vault, "get", corrupted)
    with pytest.raises(SecretCorruptedError):
        await handler.handle("acme", **signed)
    assert callback.results()["internal_error"] == 2.0
