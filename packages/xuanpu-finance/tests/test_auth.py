"""财务入口仅接受实际身份、请求绑定签名与持久防重放。"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.requests import Request
from xuanpu_finance.auth import FinanceAuthenticator
from xuanpu_finance.errors import FinanceError
from xuanpu_spec.call_context import request_fingerprint


class ReplayStore:
    """身份单元测试隔离真实存储，真实唯一约束另做 PostgreSQL 验收。"""

    def __init__(self) -> None:
        """记录请求绑定值。"""
        self.values: dict[tuple[str, str], tuple[str, str, str]] = {}

    async def claim_context(
        self,
        issuer: str,
        jti: str,
        subject: str,
        request_hash: str,
        idempotency_key: str,
        expires_at: float,
    ) -> None:
        """拒绝同一令牌被换键或改内容。"""
        key, value = (issuer, jti), (subject, request_hash, idempotency_key)
        if key in self.values and self.values[key] != value:
            raise FinanceError("context_replay", 401, "请求上下文不能用于其它请求")
        self.values[key] = value


class ActiveGateway:
    """在线授权目录边界。"""

    def __init__(self, active: bool = True) -> None:
        """配置实际目录回复。"""
        self.enabled = active

    async def active(self, who: Any) -> bool:
        """返回在线核对结果。"""
        return self.enabled


def signed_request(
    key: Ed25519PrivateKey,
    *,
    idem: str = "one",
    signed_idem: str = "one",
    age: int = 0,
    subject: str = "alice",
    extra: dict[str, Any] | None = None,
    bearer: str = "service-test",
) -> Request:
    """构造签名与真实请求，允许故障注入不修改受测实现。"""
    now = int(time.time()) - age
    payload: dict[str, Any] = {
        "iss": "platform",
        "aud": "finance",
        "sub": subject,
        "tenant_id": "acme",
        "session_id": "s1",
        "call_id": "c1",
        "iat": now,
        "exp": now + 60,
        "jti": "j1",
        "request_sha256": request_fingerprint(
            "POST", "/v1/finance/invoice-imports", [], {}, signed_idem, "application/json"
        ),
    }
    payload.update(extra or {})
    token = jwt.encode(payload, key, algorithm="EdDSA", headers={"kid": "key1"})

    async def receive() -> dict[str, Any]:
        """提供真实 JSON 原始字节。"""
        return {"type": "http.request", "body": b"{}", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/v1/finance/invoice-imports",
            "query_string": b"",
            "headers": [
                (b"authorization", f"Bearer {bearer}".encode()),
                (b"x-xuanpu-context", token.encode()),
                (b"idempotency-key", idem.encode()),
                (b"content-type", b"application/json"),
            ],
        },
        receive=receive,
    )


@pytest.fixture
def auth_env() -> tuple[FinanceAuthenticator, Ed25519PrivateKey]:
    """实际 Ed25519 密钥与目录／存储边界组合。"""
    key = Ed25519PrivateKey.generate()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return FinanceAuthenticator(
        service_token="service-test",
        issuer="platform",
        audience="finance",
        public_keys={"key1": public},
        store=ReplayStore(),
        gateway=ActiveGateway(),
    ), key


async def test_valid_request_and_same_context_replay(auth_env: Any) -> None:
    """合法同内容重放保持原主体。"""
    auth, key = auth_env
    who = await auth.authenticate(signed_request(key))
    replay = await auth.authenticate(signed_request(key))
    assert who.tenant_id == "acme" and who.user_id == "alice" and replay == who


@pytest.mark.parametrize(
    "changes",
    [
        {"idem": "changed"},
        {"age": 120},
        {"bearer": "incorrect"},
        {"extra": {"aud": "other"}},
        {"extra": {"exp": int(time.time()) + 3600}},
        {"subject": ""},
        {"extra": {"tenant_id": ""}},
    ],
)
async def test_invalid_context_is_rejected(auth_env: Any, changes: dict[str, Any]) -> None:
    """签名上下文不是任意身份或长期通行证。"""
    auth, key = auth_env
    with pytest.raises(FinanceError):
        await auth.authenticate(signed_request(key, **changes))


async def test_signed_new_key_cannot_reuse_jti(auth_env: Any) -> None:
    """即使重新签名，同 jti 也不能换幂等键。"""
    auth, key = auth_env
    await auth.authenticate(signed_request(key))
    with pytest.raises(FinanceError):
        await auth.authenticate(signed_request(key, idem="two", signed_idem="two"))


async def test_disabled_employee_cannot_start_command(auth_env: Any) -> None:
    """有效签名不能绕过实际员工停用。"""
    auth, key = auth_env
    auth.gateway.enabled = False
    with pytest.raises(FinanceError):
        await auth.authenticate(signed_request(key))
