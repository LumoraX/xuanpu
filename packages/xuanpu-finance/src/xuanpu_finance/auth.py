"""独立财务入口的服务认证、请求绑定签名与在线员工核对。"""

from __future__ import annotations

import hmac
import json
import time
from typing import TYPE_CHECKING, Any, Protocol

import jwt
from xuanpu_spec.call_context import request_fingerprint

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FinancePrincipal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.requests import Request


class ContextStore(Protocol):
    """持久防重放边界；不能使用每进程内存代替。"""

    async def claim_context(
        self,
        issuer: str,
        jti: str,
        subject: str,
        request_hash: str,
        idempotency_key: str,
        expires_at: float,
    ) -> None:
        """原子认领发行者／令牌唯一键并比较原请求。"""
        ...


class IdentityGateway(Protocol):
    """在线身份核对接口。"""

    async def active(self, who: FinancePrincipal) -> bool:
        """确认真实员工尚未停用。"""
        ...


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复 JSON 键，避免签名和业务模型解释不同。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 字段重复")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    """拒绝 JSON 的非有限扩展数值。"""
    raise ValueError("JSON 不允许非有限数值")


class FinanceAuthenticator:
    """服务密钥只证明服务身份，员工身份由绑定请求的签名与在线目录证明。"""

    def __init__(
        self,
        *,
        service_token: str,
        issuer: str,
        audience: str,
        public_keys: Mapping[str, str],
        store: ContextStore,
        gateway: IdentityGateway,
    ) -> None:
        """显式注入信任材料，不从运行环境隐式取值。"""
        if not service_token or not issuer or not audience or not public_keys:
            raise ValueError("财务认证信任配置不能为空")
        self.service_token, self.issuer, self.audience = service_token, issuer, audience
        self.public_keys, self.store, self.gateway = dict(public_keys), store, gateway

    def _claims(self, token: str) -> dict[str, Any]:
        """只接受配置内公钥、固定算法与短期完整载荷。"""
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if not isinstance(kid, str) or kid not in self.public_keys:
                raise ValueError("未知签名公钥")
            claims: dict[str, Any] = jwt.decode(
                token,
                self.public_keys[kid],
                algorithms=["EdDSA"],
                issuer=self.issuer,
                audience=self.audience,
                leeway=5,
                options={
                    "require": [
                        "iss",
                        "aud",
                        "sub",
                        "tenant_id",
                        "session_id",
                        "call_id",
                        "iat",
                        "exp",
                        "jti",
                        "request_sha256",
                    ]
                },
            )
            for field in ("sub", "tenant_id", "session_id", "call_id", "jti"):
                value = claims[field]
                if not isinstance(value, str) or not value.strip() or len(value) > 512:
                    raise ValueError("签名身份字段非法")
            if claims.get("origin", "agent") not in ("agent", "human"):
                raise ValueError("签名来源非法")
            issued, expires = claims["iat"], claims["exp"]
            if (
                type(issued) is not int
                or type(expires) is not int
                or not 0 < expires - issued <= 60
                or issued > time.time() + 5
            ):
                raise ValueError("签名期限非法")
            return claims
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as error:
            raise FinanceError("invalid_context", 401, "请求上下文无效") from error

    async def authenticate(self, request: Request) -> FinancePrincipal:
        """先验证实际请求和持久重放，再在线复核员工。"""
        for name in ("authorization", "x-xuanpu-context", "idempotency-key", "content-type"):
            if len(request.headers.getlist(name)) > 1:
                raise FinanceError("ambiguous_headers", 401, "认证请求头不能重复")
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {self.service_token}"
        if not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise FinanceError("unauthenticated", 401, "服务凭据无效")
        token = request.headers.get("x-xuanpu-context", "")
        if not token or len(token) > 16384:
            raise FinanceError("invalid_context", 401, "缺少有效请求上下文")
        claims = self._claims(token)
        raw = await request.body()
        if len(raw) > 1024 * 1024:
            raise FinanceError("request_too_large", 413, "财务请求超过大小限制")
        try:
            body = (
                json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                if raw
                else None
            )
            fingerprint = request_fingerprint(
                request.method,
                request.url.path,
                list(request.query_params.multi_items()),
                body,
                request.headers.get("idempotency-key", ""),
                request.headers.get("content-type", ""),
            )
        except (ValueError, UnicodeDecodeError) as error:
            raise FinanceError("invalid_request", 400, "请求 JSON 无效") from error
        if not hmac.compare_digest(str(claims["request_sha256"]), fingerprint):
            raise FinanceError("request_mismatch", 401, "请求与签名不匹配")
        who = FinancePrincipal(claims["tenant_id"], claims["sub"], claims.get("origin", "agent"))
        await self.store.claim_context(
            self.issuer,
            claims["jti"],
            json.dumps(
                [who.tenant_id, who.user_id, who.origin], ensure_ascii=False, separators=(",", ":")
            ),
            fingerprint,
            request.headers.get("idempotency-key", ""),
            float(claims["exp"]),
        )
        if not await self.gateway.active(who):
            raise FinanceError("employee_unavailable", 403, "员工已停用或授权失效")
        return who
