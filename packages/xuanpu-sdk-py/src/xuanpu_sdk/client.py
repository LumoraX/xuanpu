"""平台客户端。

每个方法对应一个 HTTP 接口；错误响应统一抛 ``XuanpuError``。事件流方法返回异步迭代器，
迭代到本轮对话结束为止。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

import httpx

from xuanpu_sdk.errors import XuanpuError, XuanpuTransportError
from xuanpu_sdk.models import AuditRecord, Event, IssuedKey, PackageVersion, Session, Usage
from xuanpu_sdk.sse import parse_sse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from types import TracebackType

DEFAULT_TIMEOUT_SECONDS = 60.0


def _raise_for(response: httpx.Response) -> None:
    """错误响应 → 异常。"""
    if response.status_code < 400:
        return
    try:
        body = response.json()
        error = body["error"]
        code, message = str(error["code"]), str(error["message"])
    except (ValueError, KeyError, TypeError) as exc:
        raise XuanpuTransportError(f"HTTP {response.status_code}，响应不是平台的错误格式") from exc
    details = {key: value for key, value in error.items() if key not in ("code", "message")}
    raise XuanpuError(response.status_code, code, message, details)


class _Http:
    """带凭据的请求封装。"""

    def __init__(self, base_url: str, api_key: str, timeout: float, transport: Any) -> None:
        """建立底层 HTTP 客户端。"""
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """发一个普通请求，返回 JSON 对象。"""
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise XuanpuTransportError(f"请求失败：{type(exc).__name__}") from exc
        _raise_for(response)
        body = response.json()
        if not isinstance(body, dict):
            raise XuanpuTransportError("响应不是 JSON 对象")
        return body

    async def stream(self, path: str, body: dict[str, Any]) -> AsyncIterator[Event]:
        """发一个事件流请求，逐条产出内核事件，直到本轮对话结束。"""
        try:
            async with self._client.stream(
                "POST", path, json=body, timeout=httpx.Timeout(None, connect=30.0)
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    _raise_for(response)
                async for name, payload in parse_sse(response.aiter_lines()):
                    if name in ("turn", "end"):
                        continue
                    yield Event(
                        kind=str(payload["kind"]),
                        submission_id=str(payload["submission_id"]),
                        seq=int(payload["seq"]),
                        at=float(payload["at"]),
                        data=dict(payload.get("data") or {}),
                    )
        except httpx.HTTPError as exc:
            raise XuanpuTransportError(f"事件流中断：{type(exc).__name__}") from exc

    async def close(self) -> None:
        """关闭底层连接。"""
        await self._client.aclose()


class Sessions:
    """会话接口。"""

    def __init__(self, http: _Http) -> None:
        """绑定请求封装。"""
        self._http = http

    async def create(
        self, package: str, *, version: str | None = None, entry: str | None = None
    ) -> Session:
        """新建会话。"""
        body: dict[str, Any] = {"package": package}
        if version is not None:
            body["version"] = version
        if entry is not None:
            body["entry"] = entry
        return Session.from_json(await self._http.request("POST", "/v1/sessions", json=body))

    async def get(self, session_id: str) -> Session:
        """取会话。"""
        return Session.from_json(await self._http.request("GET", f"/v1/sessions/{session_id}"))

    async def list(self, *, status: str | None = None, limit: int = 50) -> list[Session]:
        """列出会话。"""
        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status
        body = await self._http.request("GET", "/v1/sessions", params=params)
        return [Session.from_json(item) for item in body["sessions"]]

    async def close(self, session_id: str) -> Session:
        """关闭会话。"""
        return Session.from_json(await self._http.request("DELETE", f"/v1/sessions/{session_id}"))

    async def pending(self, session_id: str) -> Sequence[dict[str, Any]]:
        """会话当前等待处理的审批或表单请求。"""
        body = await self._http.request("GET", f"/v1/sessions/{session_id}/pending")
        return [dict(item) for item in body["pending"]]

    async def replay(
        self, session_id: str, submission_id: str, *, after_seq: int = -1
    ) -> Sequence[dict[str, Any]]:
        """回放一轮对话已记入审计的事件（流式增量除外；回复全文在 ``turn_text`` 里）。"""
        body = await self._http.request(
            "GET",
            f"/v1/sessions/{session_id}/events",
            params={"submission_id": submission_id, "after_seq": after_seq},
        )
        return [dict(item) for item in body["events"]]

    def send(self, session_id: str, text: str) -> AsyncIterator[Event]:
        """发一条消息，逐条产出这一轮对话的事件。"""
        return self._http.stream(f"/v1/sessions/{session_id}/messages", {"text": text})

    def resume(self, session_id: str, resolutions: dict[str, Any]) -> AsyncIterator[Event]:
        """送回审批结果或表单答案，逐条产出续跑部分的事件。"""
        return self._http.stream(f"/v1/sessions/{session_id}/resume", {"resolutions": resolutions})

    async def ask(self, session_id: str, text: str) -> tuple[str, Event]:
        """发一条消息并等到本轮结束，返回 ``(拼好的回复文本, 终结事件)``。

        本轮挂起等审批时，终结事件是 ``turn_suspended``，回复文本是挂起前的部分。
        """
        parts: list[str] = []
        last: Event | None = None
        async for event in self.send(session_id, text):
            parts.append(event.text_delta)
            last = event
        if last is None or not last.terminal:
            raise XuanpuTransportError("事件流在本轮对话结束之前就断开了")
        return "".join(parts), last


class Packages:
    """业务包接口。"""

    def __init__(self, http: _Http) -> None:
        """绑定请求封装。"""
        self._http = http

    async def publish(self, archive: bytes, *, activate: bool = False) -> PackageVersion:
        """发布业务包归档。"""
        body = await self._http.request(
            "POST",
            "/v1/packages",
            params={"activate": "true" if activate else "false"},
            content=archive,
            headers={"Content-Type": "application/zip"},
        )
        return PackageVersion.from_json(body)

    async def list(self, name: str | None = None) -> list[PackageVersion]:
        """列出已发布版本。"""
        params = {"name": name} if name is not None else None
        body = await self._http.request("GET", "/v1/packages", params=params)
        return [PackageVersion.from_json(item) for item in body["packages"]]

    async def activate(self, name: str, version: str) -> None:
        """切换生效版本。"""
        await self._http.request("POST", f"/v1/packages/{name}/activate", json={"version": version})

    async def evaluate(
        self, name: str, *, version: str | None = None, names: Sequence[str] | None = None
    ) -> dict[str, Any]:
        """运行评测用例，返回报告。用例较多时会比较久，不设超时。"""
        body: dict[str, Any] = {}
        if version is not None:
            body["version"] = version
        if names is not None:
            body["names"] = list(names)
        return await self._http.request(
            "POST", f"/v1/packages/{name}/evals", json=body, timeout=httpx.Timeout(None)
        )


class Secrets:
    """密钥接口。"""

    def __init__(self, http: _Http) -> None:
        """绑定请求封装。"""
        self._http = http

    async def put(self, name: str, value: str) -> None:
        """写入或覆盖一条密钥。"""
        await self._http.request("PUT", f"/v1/secrets/{name}", json={"value": value})

    async def names(self) -> list[str]:
        """列出密钥名。"""
        body = await self._http.request("GET", "/v1/secrets")
        return [str(item) for item in body["names"]]

    async def delete(self, name: str) -> None:
        """删除一条密钥。"""
        await self._http.request("DELETE", f"/v1/secrets/{name}")


class Admin:
    """平台管理与租户管理接口。"""

    def __init__(self, http: _Http) -> None:
        """绑定请求封装。"""
        self._http = http

    async def create_tenant(
        self,
        tenant_id: str,
        name: str,
        *,
        max_active_sessions: int | None = None,
        max_daily_tokens: int | None = None,
    ) -> dict[str, Any]:
        """新建租户（平台管理员）。"""
        return await self._http.request(
            "POST",
            "/v1/admin/tenants",
            json={
                "tenant_id": tenant_id,
                "name": name,
                "quota": {
                    "max_active_sessions": max_active_sessions,
                    "max_daily_tokens": max_daily_tokens,
                },
            },
        )

    async def issue_tenant_key(
        self, tenant_id: str, name: str, *, role: str = "tenant_member"
    ) -> IssuedKey:
        """给某个租户签发 API key（平台管理员）。"""
        body = await self._http.request(
            "POST", f"/v1/admin/tenants/{tenant_id}/keys", json={"name": name, "role": role}
        )
        return IssuedKey.from_json(body)

    async def issue_key(self, name: str, *, role: str = "tenant_member") -> IssuedKey:
        """给本租户签发 API key（租户管理员）。"""
        body = await self._http.request("POST", "/v1/keys", json={"name": name, "role": role})
        return IssuedKey.from_json(body)

    async def revoke_key(self, key_id: str) -> None:
        """吊销本租户的一把 API key。"""
        await self._http.request("DELETE", f"/v1/keys/{key_id}")

    async def audit(
        self,
        *,
        session_id: str | None = None,
        kind: str | None = None,
        source: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 100,
    ) -> AsyncIterator[AuditRecord]:
        """查询审计记录，自动翻页。"""
        params: dict[str, Any] = {"limit": limit}
        for key, value in (
            ("session_id", session_id),
            ("kind", kind),
            ("source", source),
            ("since", since),
            ("until", until),
        ):
            if value is not None:
                params[key] = value
        after: int | None = 0
        while after is not None:
            body = await self._http.request(
                "GET", "/v1/audit", params={**params, "after_id": after}
            )
            for item in body["records"]:
                yield AuditRecord.from_json(item)
            after = body.get("next_after_id")

    async def usage(self, since: str, until: str) -> list[Usage]:
        """查询一段日期内的用量。"""
        body = await self._http.request("GET", "/v1/usage", params={"since": since, "until": until})
        return [Usage.from_json(item) for item in body["usage"]]


class XuanpuClient:
    """平台客户端。

    用法::

        async with XuanpuClient(url, api_key) as client:
            session = await client.sessions.create("support-desk")
            reply, last = await client.sessions.ask(session.session_id, "你好")
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Any = None,
    ) -> None:
        """
        Args:
            base_url: 平台地址。
            api_key: 凭据（根凭据或 API key）。
            timeout_seconds: 普通请求的超时；事件流不受它限制。
            transport: 自定义传输（测试时把客户端直接接到 ASGI 应用上）。
        """
        self._http = _Http(base_url, api_key, timeout_seconds, transport)
        self.sessions = Sessions(self._http)
        self.packages = Packages(self._http)
        self.secrets = Secrets(self._http)
        self.admin = Admin(self._http)

    async def health(self) -> dict[str, Any]:
        """存活探测。"""
        return await self._http.request("GET", "/healthz")

    async def close(self) -> None:
        """关闭连接。"""
        await self._http.close()

    async def __aenter__(self) -> Self:
        """进入上下文。"""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出时关闭连接。"""
        await self.close()


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "Admin", "Packages", "Secrets", "Sessions", "XuanpuClient"]
