"""飞书事件回调用例的公共夹具：按设计第 3 节的算法造样本，以及往事件地址发请求的把手。

样本在这里独立造（用 ``cryptography``，不调平台的实现）：请求体
``{"encrypt": base64(IV + AES-256-CBC(PKCS7(明文)))}``，密钥是 Encrypt Key 的 SHA-256；签名是
``sha256(timestamp + nonce + Encrypt Key + 原始请求体)`` 的十六进制。几个取值来自上一级的夹具
``feishu_app``（与 ``IdentityWorld.configure_feishu`` 写进去的一致）。上一级的夹具在这里照样可用。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from prometheus_client.parser import text_string_to_metric_families
from xuanpu_server.controlplane.models import AuditQuery
from xuanpu_server.identity import ExternalIdentity, UserRecord, new_user_id

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx
    from xuanpu_server.tenancy import TenantRole

EVENTS = "/v1/identity/feishu/events/acme"
"""租户 acme 的事件地址。"""


def _aes(key: str) -> algorithms.AES:
    """飞书的 AES 密钥：Encrypt Key 的 SHA-256。"""
    return algorithms.AES(hashlib.sha256(key.encode()).digest())


@dataclass(frozen=True)
class FeishuEventSamples:
    """飞书事件回调的样本。``app`` 是上一级的 ``FeishuApp``（App ID、两个密钥、飞书企业）。"""

    app: Any

    @property
    def encrypt_key(self) -> str:
        """Encrypt Key 的明文。"""
        key: str = self.app.encrypt_key
        return key

    @property
    def verification_token(self) -> str:
        """Verification Token 的明文。"""
        token: str = self.app.verification_token
        return token

    @staticmethod
    def encrypt_raw(key: str, data: bytes) -> str:
        """不加填充直接加密（``data`` 必须是 16 的倍数），用来造填充不对的样本。"""
        iv = secrets.token_bytes(16)
        sealer = Cipher(_aes(key), modes.CBC(iv)).encryptor()
        return base64.b64encode(iv + sealer.update(data) + sealer.finalize()).decode()

    def encrypt(self, key: str, plaintext: bytes) -> str:
        """按飞书的算法加密：PKCS7 填充、AES-256-CBC、IV 放在密文前面、base64。"""
        padder = padding.PKCS7(128).padder()
        return self.encrypt_raw(key, padder.update(plaintext) + padder.finalize())

    def sealed(self, payload: Any, *, key: str | None = None) -> bytes:
        """加密后的请求体。明文用 ASCII 转义的 JSON：孤立的代理项也能写成 ``"\\ud800"``。"""
        encrypted = self.encrypt(key or self.encrypt_key, json.dumps(payload).encode())
        return json.dumps({"encrypt": encrypted}).encode()

    def loosely_padded(self, payload: Any) -> bytes:
        """填充不合 PKCS7 的请求体：最后一个字节是 n，前面 n-1 个字节却是 0。

        只看最后一个字节去填充（飞书官方 SDK 的宽松做法）解出来恰好是合法的 JSON；严格的 PKCS7 必须
        拒绝它。
        """
        plaintext = json.dumps(payload).encode()
        while 16 - len(plaintext) % 16 < 2:
            plaintext += b" "  # 补空白（JSON 允许）让填充至少两个字节，才有「不一致」可言
        size = 16 - len(plaintext) % 16
        filler = b"\x00" * (size - 1) + bytes([size])
        return json.dumps(
            {"encrypt": self.encrypt_raw(self.encrypt_key, plaintext + filler)}
        ).encode()

    def lenient_decrypt(self, encrypted: str) -> bytes:
        """只看最后一个字节去填充的解密，用来证明 ``loosely_padded`` 的样本在宽松做法下是合法的。"""
        raw = base64.b64decode(encrypted)
        decryptor = Cipher(_aes(self.encrypt_key), modes.CBC(raw[:16])).decryptor()
        padded = decryptor.update(raw[16:]) + decryptor.finalize()
        return padded[: -padded[-1]]

    @staticmethod
    def sign(timestamp: str, nonce: str, key: str, body: bytes) -> str:
        """飞书的签名。"""
        return hashlib.sha256((timestamp + nonce + key).encode() + body).hexdigest()

    def signed_headers(self, body: bytes, *, key: str | None = None) -> dict[str, str]:
        """带签名的请求头。"""
        timestamp, nonce = str(int(time.time())), "nonce-" + secrets.token_hex(4)
        return {
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": self.sign(timestamp, nonce, key or self.encrypt_key, body),
            "Content-Type": "application/json",
        }

    def url_verification(
        self, *, token: str | None = None, challenge: str = "ajls384kdjx98XX"
    ) -> dict[str, Any]:
        """URL 验证请求解密后的样子。"""
        return {
            "challenge": challenge,
            "token": self.verification_token if token is None else token,
            "type": "url_verification",
        }

    def deleted(self, union_id: str, **overrides: Any) -> dict[str, Any]:
        """员工离职事件（2.0 版）。``overrides`` 改 header 里的项；其中的 ``status`` 改
        ``event.object.status``。"""
        status = overrides.pop("status", {"is_resigned": True, "is_frozen": False})
        header = {
            "event_id": "evt_0001",
            "token": self.verification_token,
            "create_time": "1790900000000",
            "event_type": "contact.user.deleted_v3",
            "tenant_key": self.app.tenant_key,
            "app_id": self.app.app_id,
            **overrides,
        }
        obj = {"open_id": "ou_x", "union_id": union_id, "user_id": "u1", "status": status}
        return {"schema": "2.0", "header": header, "event": {"object": obj, "old_object": {}}}


@dataclass
class Callback:
    """配了飞书的平台（``ident`` 是上一级的 ``IdentityWorld``），外加往事件地址发请求的把手。"""

    ident: Any
    samples: FeishuEventSamples

    async def post(
        self, body: bytes, *, signed: bool = True, path: str = EVENTS, key: str | None = None
    ) -> httpx.Response:
        """把一个请求体发到事件地址（不带任何平台凭据）。"""
        headers = self.samples.signed_headers(body, key=key) if signed else {}
        response: httpx.Response = await self.ident.http.post(path, content=body, headers=headers)
        return response

    async def employee(self, union_id: str, *, role: TenantRole = "tenant_member") -> UserRecord:
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
        await self.ident.store.create_user(user, identity=identity)
        return user

    def results(self) -> dict[str, float]:
        """``xuanpu_feishu_events_total`` 各个 ``result`` 的计数。"""
        text = self.ident.world.platform.metrics.render().decode()
        return {
            sample.labels["result"]: sample.value
            for family in text_string_to_metric_families(text)
            if family.name == "xuanpu_feishu_events"
            for sample in family.samples
        }

    async def audit_count(self) -> int:
        """租户 acme 的审计记录总数。"""
        found = await self.ident.world.platform.store.query_audit("acme", AuditQuery(limit=1000))
        return len(found)

    async def status_of(self, user: UserRecord) -> str:
        """员工当前的状态。"""
        current = await self.ident.store.get_user("acme", user.user_id)
        status: str = current.status
        return status

    @staticmethod
    def unauthenticated(response: httpx.Response) -> bool:
        """是不是统一的 401。"""
        return response.status_code == 401 and response.json() == {
            "error": {"code": "unauthenticated", "message": "凭据无效"}
        }


@pytest.fixture
def samples(feishu_app: Any) -> FeishuEventSamples:
    """飞书事件回调的样本（见 ``FeishuEventSamples``）。"""
    return FeishuEventSamples(feishu_app)


@pytest.fixture
async def callback(identity_world: Any, samples: FeishuEventSamples) -> AsyncIterator[Callback]:
    """配了飞书（已绑定飞书企业）的平台，样例业务包已发布。"""
    async with identity_world() as ident:
        await ident.world.publish()
        await ident.configure_feishu()
        await ident.store.bind_tenant_key("acme", "feishu", samples.app.tenant_key)
        yield Callback(ident=ident, samples=samples)


@pytest.fixture
def unbound_callback(identity_world: Any, samples: FeishuEventSamples) -> Any:
    """返回异步上下文管理器的工厂：配了飞书、**还没绑定**飞书企业的平台（没有员工登录过）。"""

    @asynccontextmanager
    async def opener() -> AsyncIterator[Callback]:
        """装配并配置飞书，不绑定飞书企业。"""
        async with identity_world() as ident:
            await ident.configure_feishu()
            yield Callback(ident=ident, samples=samples)

    return opener
