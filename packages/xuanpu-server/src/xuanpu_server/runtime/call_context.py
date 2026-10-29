"""审批闸门内的可信身份作用域与短期 Ed25519 调用签名。"""

from __future__ import annotations

import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

import jwt
from xuanpu_spec.call_context import request_fingerprint

from xuanpu_server.errors import PermissionDeniedError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from xuanpu_server.config import CallContextConfig
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.runtime.events import SessionRef
    from xuanpu_server.secrets import SecretVault
    from xuanpu_server.tenancy import Principal

_CALL: ContextVar[tuple[str | None, str] | None] = ContextVar("xuanpu_approved_call", default=None)


@contextmanager
def bind_call_context(session: SessionRef, call_id: str) -> Iterator[None]:
    """仅由过审批的闸门绑定真实调用标识，退出或取消时恢复原作用域。"""
    token = _CALL.set((session.session_id, call_id))
    try:
        yield
    finally:
        _CALL.reset(token)


class CallContextSigner:
    """用租户密钥为可信持久会话签发请求绑定的短期 JWT。"""

    def __init__(
        self,
        *,
        config: CallContextConfig,
        store: ControlPlaneStore,
        vault: SecretVault,
        now: Callable[[], float] = time.time,
    ) -> None:
        """注入部署信任配置、持久会话库、租户密钥库与时钟。"""
        self._config, self._store, self._vault, self._now = config, store, vault, now

    def require_audience(self, url: str, audience: str) -> None:
        """签名只能发给部署配置授权的精确源及受众组合。"""
        parts = urlsplit(url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            raise PermissionDeniedError("调用签名目标地址不合法")
        from xuanpu_server.identity.config import normalize_origin

        origin = normalize_origin(f"{parts.scheme}://{parts.netloc}", name="调用签名目标")
        if audience not in self._config.allowed_audiences.get(origin, []):
            raise PermissionDeniedError("调用签名目标源或受众未授权")

    async def sign(
        self,
        *,
        tenant_id: str,
        audience: str,
        url: str,
        method: str,
        query: Sequence[tuple[str, str]],
        body: Any,
        idempotency_key: str = "",
        content_type: str = "application/json",
    ) -> str:
        """从闸门索引定位持久会话；绝不采信模型、参数或索引缓存中的员工身份。"""
        self.require_audience(url, audience)
        bound = _CALL.get()
        if bound is None or not bound[0] or not bound[1]:
            raise PermissionDeniedError("调用签名需要已审批的员工会话")
        session = await self._store.get_session(tenant_id, bound[0])
        if (
            session is None
            or session.tenant_id != tenant_id
            or not session.user_id
            or session.status != "active"
        ):
            raise PermissionDeniedError("调用签名需要同租户的活动员工会话")
        return await self._encode(tenant_id=tenant_id, audience=audience, url=url, method=method,
            query=query, body=body, idempotency_key=idempotency_key, content_type=content_type,
            user_id=session.user_id, session_id=session.session_id, call_id=bound[1], origin="agent")

    async def _encode(
        self, *, tenant_id: str, audience: str, url: str, method: str,
        query: Sequence[tuple[str, str]], body: Any, idempotency_key: str,
        content_type: str, user_id: str, session_id: str, call_id: str, origin: str,
    ) -> str:
        """共享请求编码；来源由两个可信服务入口固定，绝不从请求参数读取。"""
        self.require_audience(url, audience)
        now = int(self._now())
        parts = urlsplit(url)
        claims = {
            "iss": self._config.issuer,
            "aud": audience,
            "kid": self._config.key_id,
            "sub": user_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "call_id": call_id,
            "origin": origin,
            "iat": now,
            "exp": now + 60,
            "jti": secrets.token_hex(16),
            "request_sha256": request_fingerprint(
                method, unquote(parts.path), query, body, idempotency_key, content_type
            ),
        }
        private_key = await self._vault.get(tenant_id, self._config.private_key_secret)
        try:
            return jwt.encode(
                claims, private_key, algorithm="EdDSA", headers={"kid": self._config.key_id}
            )
        except Exception:
            # 成熟库可能把密钥解析细节放进异常；不将原文传播到模型或日志。
            raise PermissionDeniedError("调用签名密钥不可用") from None


class HumanContextSigner:
    """为 cookie 人工交互签名；不查询或伪造模型持久会话。"""

    def __init__(self, *, config: CallContextConfig, vault: SecretVault,
                 now: Callable[[], float] = time.time) -> None:
        """注入部署受众、租户密钥和时钟。"""
        self._config, self._vault, self._now = config, vault, now

    def bind(self, principal: Principal, session_id: str, call_id: str) -> _HumanRequestSigner:
        """仅接受人工网关认证后的员工主体与服务端交互标识。"""
        if (not principal.user_id or not principal.tenant_id or principal.key_id is not None
                or principal.system is not None or principal.role == "platform_admin"):
            raise PermissionDeniedError("人工操作需要员工登录")
        if not session_id.startswith("interactive:") or not call_id:
            raise PermissionDeniedError("人工交互标识不合法")
        return _HumanRequestSigner(self, principal, session_id, call_id)


class _HumanRequestSigner(CallContextSigner):
    """单个已认证人工 HTTP 调用的不可变签名主体。"""

    def __init__(self, parent: HumanContextSigner, principal: Principal,
                 session_id: str, call_id: str) -> None:
        """只初始化共享编码依赖，故意不携带模型会话库。"""
        self._config, self._vault, self._now = parent._config, parent._vault, parent._now
        self._principal, self._session_id, self._call_id = principal, session_id, call_id

    async def sign(self, *, tenant_id: str, audience: str, url: str, method: str,
                   query: Sequence[tuple[str, str]], body: Any, idempotency_key: str = "",
                   content_type: str = "application/json") -> str:
        """固定 human 来源并再次校验租户，签发最终实际请求。"""
        if tenant_id != self._principal.tenant_id or not self._principal.user_id:
            raise PermissionDeniedError("人工调用租户不匹配")
        return await self._encode(tenant_id=tenant_id, audience=audience, url=url, method=method,
            query=query, body=body, idempotency_key=idempotency_key, content_type=content_type,
            user_id=self._principal.user_id, session_id=self._session_id,
            call_id=self._call_id, origin="human")
