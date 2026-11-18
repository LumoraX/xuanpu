"""一轮内每次模型调用前复核能力依据（R4-B1）：分配、发布撤销或员工离职后，下一次模型请求即被拒。

- 复核对象由服务端执行空间（``ExecutionScope.execution_id``）推出：``session:<id>`` 对照会话绑定，
  ``task:<id>[:attempt:n]`` 对照任务绑定；不读客户端 metadata；
- 复核时机：模型会话打开时与每次实际发出请求前；不在每个流式块上复核（每次复核要读能力快照，
  而已在途的一次请求无法追回）；
- 未纳管的包由 ``CapabilityAdmission.check`` 直接放行，行为不变。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import taifeng

    from xuanpu_server.runtime.execution_scope import ExecutionScope

    Check = Callable[[], Awaitable[None]]
    AdmissionCheck = Callable[[str, str, Any, str], Awaitable[None]]


def capability_check(admission: AdmissionCheck | None, scope: ExecutionScope | None,
                     package: str) -> Check | None:
    """按执行空间造复核函数；没有准入服务或不是员工会话/任务的执行空间时返回 None。"""
    if admission is None or scope is None:
        return None
    kind, _, rest = scope.execution_id.partition(':')
    if kind not in ('session', 'task') or not rest:
        return None
    ref_id = rest.split(':attempt:', 1)[0]
    tenant = scope.tenant_id

    async def check() -> None:
        """对照调用绑定复核当前资格；失效即抛 PermissionDeniedError。"""
        await admission(tenant, package, kind, ref_id)

    return check


class CapabilityModelClient:
    """模型客户端包装：打开会话与每次发请求前复核能力依据。"""

    def __init__(self, inner: taifeng.ModelClient, check: Check) -> None:
        """保留内层客户端的能力声明，不改模型路由。"""
        self.inner, self.check = inner, check
        capabilities = getattr(inner, 'capabilities', None)
        if capabilities is not None:
            self.capabilities = capabilities

    def session(self, *, cancel: taifeng.CancellationToken, model: str | None = None) -> Any:
        """会话创建本身不发正文；进入与每次请求前分别复核。"""
        return _CapabilitySession(self.inner.session(cancel=cancel, model=model), self.check)


class _CapabilitySession:
    """复核失败时不发出这次请求。"""

    def __init__(self, inner: Any, check: Check) -> None:
        """只持有公开会话协议。"""
        self.inner, self.check = inner, check
        self.active = inner

    async def __aenter__(self) -> _CapabilitySession:
        """复核先于底层会话打开。"""
        await self.check()
        self.active = await self.inner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """关闭底层会话。"""
        await self.inner.__aexit__(*exc)

    async def stream(self, request: taifeng.ApiRequest) -> AsyncIterator[Any]:
        """发请求前复核；之后的流式块原样转交。"""
        await self.check()
        stream = self.active.stream(request)
        try:
            async for event in stream:
                yield event
        finally:
            closer = getattr(stream, 'aclose', None)
            if closer is not None:
                await closer()
