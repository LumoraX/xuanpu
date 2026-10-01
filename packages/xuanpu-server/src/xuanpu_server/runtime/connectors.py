"""连接器：把业务包声明的外部能力接成模型可用的工具。

连接器是业务方扩展平台能力的通道（ADR 0002），也是效应面的一个闸门（ADR 0004 决策 3）。
三种传输共用同一个闸门，每次调用依次经过：白名单 → 审批 → 调用 → 审计。

- ``stdio``：平台在这个运行时的连接器沙盒里启动业务包声明的 MCP server 进程；
- ``remote``：部署在别处、经 MCP Streamable HTTP 访问的 MCP server；
- ``http``：按业务包里的 OpenAPI 文档直接调用 HTTP 接口，每个 operation 是一个工具
  （调用细节在 ``http_api.py``）。

两种 MCP 传输的启动、列工具与调用在 ``mcp_connectors.py`` 与 ``remote_mcp.py``；这里只留
闸门、按传输分派与收尾。

凭据注入：业务包里只写密钥名；明文在启动连接器进程或发出请求时才从保管库取出，只进连接器
进程的环境（或那一次请求），不进审计、日志与错误提示。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import taifeng
from xuanpu_spec import OpenApiError, load_openapi

from xuanpu_server.audit import digest_arguments
from xuanpu_server.errors import PackageInvalidError, PlatformError, RuntimeUnavailableError
from xuanpu_server.runtime.connector_process import ObservingExecutor
from xuanpu_server.runtime.http_api import ConnectorCallError, HttpApiClient, resolve_base_url
from xuanpu_server.runtime.mcp_connectors import (
    StdioRun,
    open_mcp,
    render_result,
    stdio_environment,
    truncate,
)
from xuanpu_server.runtime.remote_mcp import RemoteRun
from xuanpu_server.runtime.sandbox import DEFAULT_BASE_ENV

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from xuanpu_spec.manifest import ConnectorSpec, Effect

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.runtime.events import SubmissionIndex
    from xuanpu_server.runtime.mcp_connectors import Invoke, McpRun, McpTool
    from xuanpu_server.secrets import SecretVault

logger = logging.getLogger(__name__)

# 副作用分类 → 内核的 effect 分类（崩溃恢复时据此决定能否安全重发）
_EFFECT_KIND: dict[str, str] = {
    "read": "pure",
    "write": "external_non_idempotent",
    "external": "external_non_idempotent",
}


@dataclass(frozen=True)
class ConnectorTool:
    """一个已接入的连接器工具。"""

    connector: str
    tool: str
    effect: Effect
    spec: taifeng.ToolSpec

    @property
    def name(self) -> str:
        """模型看到的工具名。"""
        return self.spec.name


@dataclass(frozen=True)
class _Pending:
    """已列出、等权限策略生成之后再建的工具。"""

    spec: ConnectorSpec
    tool: str
    effect: Effect
    description: str
    input_schema: dict[str, Any]
    invoke: Invoke


def _pending_mcp(spec: ConnectorSpec, tool: McpTool) -> _Pending:
    """MCP 连接器（stdio 与 remote）列出的一个工具：副作用分类按清单，不采信 server 的自述。"""
    return _Pending(
        spec=spec,
        tool=tool.name,
        effect=spec.effect_of(tool.name),
        description=tool.description,
        input_schema=tool.input_schema,
        invoke=tool.invoke,
    )


class ConnectorGate:
    """一个连接器工具的闸门：审批、调用、审计。与传输方式无关。"""

    def __init__(
        self,
        *,
        invoke: Invoke,
        connector: str,
        transport: str,
        tool: str,
        full_name: str,
        effect: Effect,
        tenant_id: str,
        policy: taifeng.PermissionPolicy,
        audit: AuditLog,
        submissions: SubmissionIndex,
        timeout_seconds: float,
    ) -> None:
        """绑定一个工具。"""
        self._invoke = invoke
        self._connector = connector
        self._transport = transport
        self._tool = tool
        self._effect = effect
        self._tenant_id = tenant_id
        self._policy = policy
        self._audit = audit
        self._submissions = submissions
        self._timeout = timeout_seconds
        self.full_name = full_name

    async def _record(
        self,
        ctx: taifeng.ToolContext,
        args: dict[str, Any],
        *,
        decision: str,
        started: float,
        is_error: bool,
        detail: str = "",
    ) -> None:
        """写一条过闸记录。参数只记摘要。"""
        await self._audit.gate(
            self._tenant_id,
            "connector_call",
            {
                "connector": self._connector,
                "transport": self._transport,
                "tool": self._tool,
                "effect": self._effect,
                "decision": decision,
                "is_error": is_error,
                "detail": detail,
                "call_id": ctx.call_id,
                "thread_id": ctx.thread_id,
                "arguments_digest": digest_arguments(args),
                "submission_id": ctx.extras.get("submission_id"),
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
            session_id=self._submissions.session_of(ctx.extras.get("submission_id"), ctx.thread_id),
        )

    async def __call__(self, args: dict[str, Any], ctx: taifeng.ToolContext) -> taifeng.ToolResult:
        """工具处理函数。"""
        started = time.monotonic()
        # 审批：需要人来定时，这里抛出挂起信号，本轮对话停在这一步；
        # 审批通过后内核带着同一个 call_id 重新进来，这次直接放行
        decision = await self._policy.check(
            taifeng.PermissionRequest(
                scope="tool_use",
                target=self.full_name,
                reason=f"调用连接器 {self._connector} 的 {self._tool}（{self._effect}）",
                metadata={"call_id": ctx.call_id, "thread_id": ctx.thread_id, "args": args},
            )
        )
        if not decision.granted:
            await self._record(
                ctx, args, decision="denied", started=started, is_error=True, detail=decision.reason
            )
            return taifeng.ToolResult.error(
                f"permission_denied: {decision.reason}", reason="permission_denied"
            )
        try:
            text, is_error = await asyncio.wait_for(self._invoke(args), timeout=self._timeout)
        except TimeoutError:
            await self._record(
                ctx, args, decision="allowed", started=started, is_error=True, detail="timeout"
            )
            return taifeng.ToolResult.error("connector_timeout", reason="timeout")
        except (taifeng.McpToolError, ConnectorCallError) as exc:
            await self._record(
                ctx, args, decision="allowed", started=started, is_error=True, detail=str(exc)
            )
            return taifeng.ToolResult.error(f"connector_error: {exc}", reason="connector_error")
        except PlatformError as exc:
            # 调用途中平台侧出了问题（比如密钥刚被删掉）：记下来，告诉模型这次调用没成
            await self._record(
                ctx, args, decision="allowed", started=started, is_error=True, detail=exc.code
            )
            return taifeng.ToolResult.error(f"{exc.code}: {exc.message}", reason=exc.code)
        await self._record(ctx, args, decision="allowed", started=started, is_error=is_error)
        if is_error:
            return taifeng.ToolResult.error(text or "connector_error", reason="tool_error")
        return taifeng.ToolResult.ok(text, connector=self._connector, tool=self._tool)


class ConnectorSet:
    """一个运行时的全部连接器。"""

    def __init__(
        self,
        *,
        tenant_id: str,
        vault: SecretVault,
        audit: AuditLog,
        submissions: SubmissionIndex,
        executor: taifeng.CommandExecutor | None,
        base_env: Mapping[str, str] = DEFAULT_BASE_ENV,
        call_timeout_seconds: float = 60.0,
        startup_timeout_seconds: float = 30.0,
    ) -> None:
        """
        Args:
            tenant_id: 所属租户。
            vault: 密钥保管库。
            audit: 审计写入口。
            submissions: submission → 会话的对照表，过闸记录据此归到会话名下。
            executor: 在连接器沙盒里起 stdio 连接器进程的执行器；None 表示没有连接器沙盒，
                这时遇到 stdio 连接器直接报错，不在平台节点上起进程。
            base_env: 连接器进程的基础环境（由组装方给出，含指向连接器工作目录的
                ``HOME`` / ``TMPDIR`` / ``WORKSPACE``）；清单声明的变量与密钥加在它上面。
            call_timeout_seconds: 单次工具调用的超时（http 与 remote 连接器另受清单里的
                超时约束，取小的）。
            startup_timeout_seconds: 启动（或连上）连接器并列出工具的超时。
        """
        self._tenant_id = tenant_id
        self._vault = vault
        self._audit = audit
        self._submissions = submissions
        self._executor = executor
        self._base_env = dict(base_env)
        self._call_timeout = call_timeout_seconds
        self._startup_timeout = startup_timeout_seconds
        # stdio 与 remote 两种 MCP 连接器的运行实例；先登记再启动，启动到一半失败也归 close() 收拾
        self._mcp: list[McpRun] = []
        self._http_clients: list[HttpApiClient] = []
        self._pending: list[_Pending] = []

    async def start(self, spec: ConnectorSpec, *, package_root: Path) -> dict[str, Effect]:
        """接入一个连接器并列出它的工具，返回「完整工具名 → 副作用分类」。

        此时还不生成工具：工具的处理函数要用到权限策略，而策略要等所有连接器的工具
        都列出来之后才能生成。

        Raises:
            SecretMissingError: 引用的密钥尚未配置。
            RuntimeUnavailableError: stdio 连接器没有连接器沙盒可用；stdio 或 remote 连接器
                起不来、连不上，或没有提供清单里声明的工具。
            PackageInvalidError: OpenAPI 文档读不出来，或 stdio 连接器要设置动态链接器变量。
        """
        if spec.transport == "http":
            found = await self._start_http(spec, package_root)
        else:
            if spec.transport == "remote":
                tools = await self._start_remote(spec)
            else:
                tools = await self._start_stdio(spec, str(package_root))
            found = [_pending_mcp(spec, tool) for tool in tools]
        self._pending.extend(found)
        return {spec.tool_name(item.tool): item.effect for item in found}

    async def _start_stdio(self, spec: ConnectorSpec, cwd: str) -> list[McpTool]:
        """在连接器沙盒里启动 MCP server 进程并列出工具。

        ``cwd`` 是业务包目录：本机与容器两种沙盒里，业务包目录都与宿主机同路径。

        Raises:
            RuntimeUnavailableError: 没有连接器沙盒，或连接器起不来、缺工具。
        """
        if self._executor is None:
            raise RuntimeUnavailableError(
                f"连接器 {spec.name!r} 是 stdio 连接器，但这个运行时没有连接器沙盒"
            )
        env, secrets = await stdio_environment(
            spec, base_env=self._base_env, tenant_id=self._tenant_id, vault=self._vault
        )
        run = StdioRun(name=spec.name, secrets=secrets, observer=ObservingExecutor(self._executor))
        self._mcp.append(run)
        spawn = functools.partial(
            run.spawn, spec.command, env=env, cwd=cwd, request_timeout=self._call_timeout
        )
        return await open_mcp(run, spec, spawn, timeout=self._startup_timeout)

    async def _start_remote(self, spec: ConnectorSpec) -> list[McpTool]:
        """连上远端 MCP server 并列出工具。不进沙盒：它本来就是一次网络调用。

        Raises:
            SecretMissingError: 引用的密钥尚未配置。
            RuntimeUnavailableError: 连不上、握手或列工具失败、超过启动时限，或缺工具。
        """
        assert spec.url is not None
        if spec.auth is not None:
            # 组装时就确认凭据已配置：与另外两种连接器一致，缺密钥时对话起不来，而不是
            # 跑到一半才失败。取值不保留，每次请求发出时由凭据传输现取
            await self._vault.get(self._tenant_id, spec.auth.secret)
        run = RemoteRun(
            name=spec.name,
            url=spec.url,
            auth=spec.auth,
            tenant_id=self._tenant_id,
            vault=self._vault,
        )
        self._mcp.append(run)
        connect = functools.partial(run.connect, request_timeout=self._timeout_of(spec))
        return await open_mcp(run, spec, connect, timeout=self._startup_timeout)

    def _timeout_of(self, spec: ConnectorSpec) -> float:
        """单次调用的超时：http 与 remote 连接器按清单的 ``timeoutSeconds`` 与平台超时取小的。"""
        if spec.transport in ("http", "remote"):
            return min(spec.timeout_seconds, self._call_timeout)
        return self._call_timeout

    async def _start_http(self, spec: ConnectorSpec, package_root: Path) -> list[_Pending]:
        """读 OpenAPI 文档，把白名单内的 operation 列成工具。不发起任何网络请求。"""
        assert spec.openapi is not None
        try:
            document = await asyncio.to_thread(load_openapi, package_root / spec.openapi)
        except OpenApiError as exc:
            raise PackageInvalidError(f"连接器 {spec.name!r} 的 OpenAPI 文档：{exc}") from exc
        missing = [tool.name for tool in spec.tools if tool.name not in document.operations]
        if missing:
            raise RuntimeUnavailableError(
                f"连接器 {spec.name!r} 的 OpenAPI 文档里没有这些 operation：{', '.join(missing)}"
            )
        if spec.auth is not None:
            # 组装时就确认凭据已配置：与 stdio 连接器一致，缺密钥时对话起不来，
            # 而不是跑到一半才失败。
            # 取值不保留，调用时再现取
            await self._vault.get(self._tenant_id, spec.auth.secret)
        client = HttpApiClient(
            base_url=resolve_base_url(spec, document),
            auth=spec.auth,
            tenant_id=self._tenant_id,
            vault=self._vault,
            timeout_seconds=self._timeout_of(spec),
        )
        self._http_clients.append(client)

        def invoker(operation_id: str) -> Invoke:
            operation = document.operations[operation_id]

            async def invoke(args: dict[str, Any]) -> tuple[str, bool]:
                text, is_error = await client.call(operation, args)
                return truncate(text), is_error

            return invoke

        found: list[_Pending] = []
        for operation_id, operation in document.operations.items():
            if not spec.allows(operation_id):
                continue
            found.append(
                _Pending(
                    spec=spec,
                    tool=operation_id,
                    effect=spec.effect_of(operation_id, read_only=operation.read_only),
                    description=operation.summary or f"{operation.method.upper()} {operation.path}",
                    input_schema=operation.input_schema(),
                    invoke=invoker(operation_id),
                )
            )
        return found

    def build_tools(self, policy: taifeng.PermissionPolicy) -> list[ConnectorTool]:
        """用最终的权限策略生成全部工具。"""
        tools: list[ConnectorTool] = []
        for item in self._pending:
            timeout = self._timeout_of(item.spec)
            gate = ConnectorGate(
                invoke=item.invoke,
                connector=item.spec.name,
                transport=item.spec.transport,
                tool=item.tool,
                full_name=item.spec.tool_name(item.tool),
                effect=item.effect,
                tenant_id=self._tenant_id,
                policy=policy,
                audit=self._audit,
                submissions=self._submissions,
                timeout_seconds=timeout,
            )
            tools.append(
                ConnectorTool(
                    connector=item.spec.name,
                    tool=item.tool,
                    effect=item.effect,
                    spec=taifeng.ToolSpec(
                        name=gate.full_name,
                        description=item.description,
                        input_schema=item.input_schema,
                        handler=gate,
                        parallel_safe=item.effect == "read",
                        # 内核自己的超时留出余量，让闸门先超时并写下审计
                        timeout_seconds=timeout + 5.0,
                        effect_kind=_EFFECT_KIND[item.effect],
                        can_suspend=True,
                    ),
                )
            )
        return tools

    async def close(self) -> None:
        """关闭全部连接器：stdio 进程、远端 MCP 会话与 HTTP 连接。某一个失败不影响其余的。"""
        runs, self._mcp = self._mcp, []
        http, self._http_clients = self._http_clients, []
        self._pending = []
        for closer in [run.close for run in runs] + [client.close for client in http]:
            try:
                await closer()
            except Exception:
                logger.exception("关闭连接器失败")


__all__ = [
    "ConnectorCallError",
    "ConnectorGate",
    "ConnectorSet",
    "ConnectorTool",
    "render_result",
    "truncate",
]
