"""公开模型与上下文协议的双重来源检查，不访问内核私有接口。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import taifeng

from xuanpu_server.errors import SharedAccessRevokedError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from xuanpu_server.runtime.execution_scope import ExecutionScope
    from xuanpu_server.shared_matters.context_guard import SharedContexts
    from xuanpu_server.tenancy import Principal

    Check = Callable[[], Awaitable[object]]

SHARED_READ_TOOLS = ('read_shared_context', 'read_shared_material')


async def source_binding(contexts: SharedContexts | None, scope: ExecutionScope | None) -> RuntimeSourceBinding | None:
    """只有服务器构造的普通员工会话参与共享阅读，R2任务不提供此入口。"""
    if contexts is None or scope is None or scope.owner_id.startswith('service:') or not scope.execution_id.startswith('session:'):
        return None
    from xuanpu_server.tenancy import Principal
    who = Principal(role='tenant_member', tenant_id=scope.tenant_id, user_id=scope.owner_id, key_id=None)
    session_id = scope.execution_id.removeprefix('session:')
    identifiers = await contexts.check_runtime(who, session_id)
    return RuntimeSourceBinding(contexts, who, session_id, protected=bool(identifiers))


class RuntimeSourceBinding:
    """身份来自已授权的ExecutionScope，客户端metadata不提供执行身份。"""
    def __init__(self, contexts: SharedContexts, who: Principal, session_id: str, *, protected: bool) -> None:
        """记录运行时是否按受保护只读模式装配。"""
        self.contexts, self.who, self.session_id, self.protected = contexts, who, session_id, protected

    async def check(self) -> tuple[str, ...]:
        """来源在装配后才被绑定时拒绝旧工具集合，不复用已有沙盒权限。"""
        identifiers = await self.contexts.check_runtime(self.who, self.session_id)
        if bool(identifiers) != self.protected:
            raise SharedAccessRevokedError('来源与会话模式不一致，请开始新会话')
        return identifiers


class SourcePermissionPolicy(taifeng.PermissionPolicy):
    """受保护阅读模式只读已选来源，不把共享正文送入脚本、文件或连接器。"""
    def __init__(self, binding: RuntimeSourceBinding) -> None:
        """源选择不能提升已有普通业务沙盒权限。"""
        super().__init__(rules=[], default_mode='deny')
        self.binding = binding

    async def check(self, request: taifeng.PermissionRequest) -> taifeng.PermissionDecision:
        """未知操作和所有派生执行均拒绝，即使模型提出或旧工具确认曾允许。"""
        await self.binding.check()
        allowed = request.scope == 'tool_use' and request.target in SHARED_READ_TOOLS
        return taifeng.PermissionDecision(granted=allowed, mode='allow' if allowed else 'deny',
            reason='读取已原生选择的共享来源' if allowed else '共享阅读模式不开放脚本、文件写入、连接器或子任务')


class SourceContextEngine:
    """在公开上下文装配槽位检查来源；不改写或摘要原始历史。"""
    name = 'xuanpu-current-shared-permissions'

    def __init__(self, check: Check) -> None:
        """固定服务端检查，不接受模型可编辑的权限字段。"""
        self.check = check

    async def assemble(self, request: taifeng.AssembleRequest) -> None:
        """装配前检查；返回None沿用原视图。受保护运行时另行关闭全文capture。"""
        await self.check()

    async def after_turn(self, update: taifeng.TurnUpdate) -> None:
        """轮后回调不承担授权，因为内核允许它尽力失败。"""


class SourceModelClient:
    """每次实际模型stream再次检查，覆盖视图缓存、内部重试和子采样。"""
    def __init__(self, inner: taifeng.ModelClient, check: Check) -> None:
        """保留原模型能力及R2预算包装，不改变模型路由。"""
        self.inner, self.check = inner, check
        capabilities = getattr(inner, 'capabilities', None)
        if capabilities is not None:
            self.capabilities = capabilities

    def session(self, *, cancel: taifeng.CancellationToken, model: str | None = None) -> Any:
        """会话创建尚不发送正文，进入前和每次stream分别复核。"""
        return _SourceSession(self.inner.session(cancel=cancel, model=model), self.check)


class _SourceSession:
    """来源失效时停止迭代，不能把旧正文或新输出继续交给下游。"""
    def __init__(self, inner: Any, check: Check) -> None:
        """仅持有公开会话协议，不保存输入正文副本。"""
        self.inner, self.check = inner, check
        self.active = inner

    async def __aenter__(self) -> _SourceSession:
        """权限先于底层会话打开。"""
        await self.check()
        self.active = await self.inner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """关闭底层模型连接，已发送的字节不宣称能追回。"""
        await self.inner.__aexit__(*exc)

    async def stream(self, request: taifeng.ApiRequest) -> AsyncIterator[Any]:
        """发送前与每个返回块复核，失效后尽力取消在途流。"""
        await self.check()
        stream = self.active.stream(request)
        try:
            async for event in stream:
                await self.check()
                yield event
        finally:
            closer = getattr(stream, 'aclose', None)
            if closer is not None:
                await closer()
