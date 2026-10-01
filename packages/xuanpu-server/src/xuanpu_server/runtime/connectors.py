"""连接器：把业务包声明的外部能力接成模型可用的工具。

连接器是业务方扩展平台能力的通道（ADR 0002），也是效应面的一个闸门（ADR 0004 决策 3）。
两种传输共用同一个闸门，每次调用依次经过：白名单 → 审批 → 调用 → 审计。

- ``stdio``：平台启动业务包声明的 MCP server 进程。只用内核稳定层的 ``McpStdioClient``
  自行包装工具；进程经执行器起在这个运行时的连接器沙盒里（只读业务包目录、只写连接器
  自己的工作目录、可出网，见 ``sandbox.py``），不会退回到在平台节点上直接起。
- ``http``：按业务包里的 OpenAPI 文档直接调用 HTTP 接口，每个 operation 是一个工具
  （调用细节在 ``http_api.py``）。

凭据注入：业务包里只写密钥名；明文在启动连接器进程或发出 HTTP 请求时才从保管库取出，
只进连接器进程的环境（或 HTTP 请求），不进审计、日志与错误提示。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import taifeng
from xuanpu_spec import OpenApiError, load_openapi

from xuanpu_server.audit import digest_arguments
from xuanpu_server.errors import PackageInvalidError, PlatformError, RuntimeUnavailableError
from xuanpu_server.redaction import redact, tail
from xuanpu_server.runtime.connector_process import ObservingExecutor
from xuanpu_server.runtime.http_api import ConnectorCallError, HttpApiClient, resolve_base_url
from xuanpu_server.runtime.sandbox import DEFAULT_BASE_ENV

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from xuanpu_spec.manifest import ConnectorSpec, Effect

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.runtime.events import SubmissionIndex
    from xuanpu_server.secrets import SecretVault

logger = logging.getLogger(__name__)

# 动态链接器读取的变量前缀，不分大小写。本机沙盒的启动器（bwrap 等）在沙盒之外拿到连接器的
# 整份环境，这类变量能让它在沙盒外加载任意代码。规范校验里有同一道检查；这里再拦一次，
# 挡住规范加上这道检查之前发布的包，以及绕过规范构造出来的清单
_LOADER_ENV_PREFIXES = ("LD_", "DYLD_")

_MAX_OUTPUT_CHARS = 64 * 1024

# 排障日志里记连接器 stderr 的末尾多少字符（脱敏之后再截）
_LOGGED_STDERR_CHARS = 2048
# 启动失败时等进程退出片刻，让退出码与最后的 stderr 落定
_SETTLE_SECONDS = 1.0

# 副作用分类 → 内核的 effect 分类（崩溃恢复时据此决定能否安全重发）
_EFFECT_KIND: dict[str, str] = {
    "read": "pure",
    "write": "external_non_idempotent",
    "external": "external_non_idempotent",
}

# 一次调用的实现：参数 → (给模型看的文本, 是否出错)
Invoke = Callable[[dict[str, Any]], Awaitable[tuple[str, bool]]]


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


def truncate(text: str) -> str:
    """给模型看的文本有上限，超出部分截掉并说明。"""
    if len(text) > _MAX_OUTPUT_CHARS:
        return text[:_MAX_OUTPUT_CHARS] + f"\n…[共 {len(text)} 字符，已截断]"
    return text


def render_result(result: Mapping[str, Any]) -> tuple[str, bool]:
    """把 MCP 调用结果整理成给模型看的文本。非文本内容给出占位，不静默丢弃。"""
    parts: list[str] = []
    content = result.get("content")
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        else:
            parts.append(f"[{kind or '未知'} 内容，未展示]")
    structured = result.get("structuredContent")
    if structured is not None and not parts:
        parts.append(json.dumps(structured, ensure_ascii=False))
    return truncate("\n".join(parts)), bool(result.get("isError"))


@dataclass
class _StdioRun:
    """一个 stdio 连接器的进程、客户端与排障线索。

    Attributes:
        name: 连接器名。
        secrets: 这次注入给它的密钥明文，日志与错误文本按它们脱敏。
        observer: 起它的执行器（记下进程与 stderr 末尾）。
        client: 握手成功后的客户端；启动没走完时为 None（进程可能已经起来了）。
    """

    name: str
    secrets: tuple[str, ...]
    observer: ObservingExecutor
    client: taifeng.McpStdioClient | None = None

    def redact(self, text: str) -> str:
        """按这个连接器的密钥脱敏。"""
        return redact(text, self.secrets)

    async def diagnostics(self, exc: BaseException | None, *, settle: bool = False) -> str:
        """脱敏后的排障线索：异常文本、退出码（拿得到的话）、stderr 末尾约 2 KiB。

        各段都用 repr 写出，连接器输出里的换行不会在日志里伪造出新的一行。

        Args:
            exc: 这次失败的异常。
            settle: 先等进程退出片刻（至多 ``_SETTLE_SECONDS``）再取退出码与 stderr。
        """
        parts: list[str] = []
        if exc is not None:
            text = self.redact(str(exc))
            parts.append(f"{type(exc).__name__}: {text!r}" if text else type(exc).__name__)
        process = self.observer.process
        if process is None:
            return "；".join([*parts, "进程没有起来"])
        if settle and process.returncode is None:
            # 进程还没退出（例如卡在握手上）就不再等：退出码记「未退出」，这不是错误
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(process.wait()), timeout=_SETTLE_SECONDS)
        code = process.returncode
        parts.append("退出码 " + ("未退出" if code is None else str(code)))
        stderr = tail(self.redact(process.stderr_text()), _LOGGED_STDERR_CHARS)
        parts.append(f"stderr 末尾 {stderr!r}")
        return "；".join(parts)

    def invoker(self, tool: str) -> Invoke:
        """调用这个连接器的某个工具。须在握手成功之后调用。

        调用失败（工具报错、请求超时、进程退出断开……）时，日志记脱敏后的排障线索；交给
        模型与审计的文本同样按值脱敏（``ConnectorCallError``）。
        """
        client = self.client
        assert client is not None

        async def invoke(args: dict[str, Any]) -> tuple[str, bool]:
            try:
                result = await client.call_tool(tool, args)
            except (taifeng.McpToolError, RuntimeError, OSError) as exc:
                # 连接断开多半是进程退出了：等它片刻，好记下退出码与遗言
                settle = not isinstance(exc, taifeng.McpToolError)
                logger.warning(
                    "连接器 %s 的工具 %s 调用失败：%s",
                    self.name,
                    tool,
                    await self.diagnostics(exc, settle=settle),
                )
                raise ConnectorCallError(self.redact(str(exc))) from None
            return render_result(result)

        return invoke

    async def close(self) -> None:
        """关闭连接器：有客户端就关客户端（它会等进程退出）；启动没走完的直接强杀进程。

        启动被取消（例如启动时限到了）时内核不会关掉已经起来的进程，只能在这里强杀。
        """
        if self.client is not None:
            await self.client.close()
        else:
            await self.observer.kill()


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
            call_timeout_seconds: 单次工具调用的超时（HTTP 连接器另受清单里的超时约束）。
            startup_timeout_seconds: 启动连接器并列出工具的超时。
        """
        self._tenant_id = tenant_id
        self._vault = vault
        self._audit = audit
        self._submissions = submissions
        self._executor = executor
        self._base_env = dict(base_env)
        self._call_timeout = call_timeout_seconds
        self._startup_timeout = startup_timeout_seconds
        self._stdio: list[_StdioRun] = []
        self._http_clients: list[HttpApiClient] = []
        self._pending: list[_Pending] = []

    async def start(self, spec: ConnectorSpec, *, package_root: Path) -> dict[str, Effect]:
        """接入一个连接器并列出它的工具，返回「完整工具名 → 副作用分类」。

        此时还不生成工具：工具的处理函数要用到权限策略，而策略要等所有连接器的工具
        都列出来之后才能生成。

        Raises:
            SecretMissingError: 引用的密钥尚未配置。
            RuntimeUnavailableError: stdio 连接器没有连接器沙盒可用、起不来，或没有提供
                清单里声明的工具。
            PackageInvalidError: OpenAPI 文档读不出来，或 stdio 连接器要设置动态链接器变量。
        """
        if spec.transport == "http":
            found = await self._start_http(spec, package_root)
        else:
            found = await self._start_stdio(spec, str(package_root))
        self._pending.extend(found)
        return {spec.tool_name(item.tool): item.effect for item in found}

    async def _environment(self, spec: ConnectorSpec) -> tuple[dict[str, str], tuple[str, ...]]:
        """连接器进程的完整环境：基础环境加清单声明的变量，密钥在此解析成明文。

        Returns:
            完整环境，以及其中的密钥明文（排障日志按它们脱敏）。

        Raises:
            PackageInvalidError: 清单要设置动态链接器变量（``LD_*``、``DYLD_*``）。
            SecretMissingError: 引用的密钥尚未配置。
        """
        blocked = sorted(name for name in spec.env if name.upper().startswith(_LOADER_ENV_PREFIXES))
        if blocked:
            raise PackageInvalidError(
                f"连接器 {spec.name!r} 的 env 不能设置动态链接器变量（LD_*、DYLD_*）："
                f"{', '.join(blocked)}；连接器进程由沙盒外的启动器拉起，这类变量会让它在沙盒之外"
                "加载任意代码"
            )
        env = dict(self._base_env)
        secrets: list[str] = []
        for name, item in spec.env.items():
            if item.secret is not None:
                env[name] = await self._vault.get(self._tenant_id, item.secret)
                secrets.append(env[name])
            else:
                assert item.value is not None
                env[name] = item.value
        return env, tuple(secrets)

    async def _start_stdio(self, spec: ConnectorSpec, cwd: str) -> list[_Pending]:
        """在连接器沙盒里启动 MCP server 进程并列出工具。

        ``cwd`` 是业务包目录：本机与容器两种沙盒里，业务包目录都与宿主机同路径。

        Raises:
            RuntimeUnavailableError: 没有连接器沙盒，或连接器起不来、缺工具。
        """
        if self._executor is None:
            raise RuntimeUnavailableError(
                f"连接器 {spec.name!r} 是 stdio 连接器，但这个运行时没有连接器沙盒"
            )
        env, secrets = await self._environment(spec)
        run = _StdioRun(name=spec.name, secrets=secrets, observer=ObservingExecutor(self._executor))
        # 先登记再启动：启动到一半失败，进程也归 close() 收拾
        self._stdio.append(run)
        try:
            listed = await asyncio.wait_for(
                self._connect(run, spec, env, cwd), timeout=self._startup_timeout
            )
        except Exception as exc:
            # 外部进程的失败方式不可穷举（起不来、握手超时、协议版本不符、列不出工具……），
            # 统一归为运行时不可用。返回的提示只带异常类型：异常文本可能含有注入进去的凭据。
            # 日志里记按值脱敏后的排障线索；不带原异常（from None），免得别处打印堆栈时
            # 把未脱敏的异常文本带出去
            logger.error(
                "连接器 %s 启动失败：%s", spec.name, await run.diagnostics(exc, settle=True)
            )
            raise RuntimeUnavailableError(
                f"连接器 {spec.name!r} 启动失败：{type(exc).__name__}"
            ) from None
        missing = [tool.name for tool in spec.tools if tool.name not in listed]
        if missing:
            logger.error(
                "连接器 %s 缺少声明的工具 %s：%s",
                spec.name,
                ", ".join(missing),
                await run.diagnostics(None),
            )
            raise RuntimeUnavailableError(
                f"连接器 {spec.name!r} 没有提供清单里声明的工具：{', '.join(missing)}"
            )
        found: list[_Pending] = []
        for name, meta in listed.items():
            if not spec.allows(name):
                continue
            schema = meta.get("inputSchema")
            found.append(
                _Pending(
                    spec=spec,
                    tool=name,
                    effect=spec.effect_of(name),
                    description=str(meta.get("description") or f"{spec.name} / {name}"),
                    input_schema=schema if isinstance(schema, dict) else {"type": "object"},
                    invoke=run.invoker(name),
                )
            )
        return found

    async def _connect(
        self, run: _StdioRun, spec: ConnectorSpec, env: dict[str, str], cwd: str
    ) -> dict[str, dict[str, Any]]:
        """起进程、握手、列出工具。握手一成功就把客户端记到 ``run`` 上，之后的失败由
        ``close()`` 关掉它。"""
        run.client = await taifeng.McpStdioClient.spawn(
            list(spec.command),
            env=env,
            cwd=cwd,
            executor=run.observer,
            request_timeout_seconds=self._call_timeout,
        )
        return {str(tool["name"]): tool for tool in await run.client.list_tools() if "name" in tool}

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
            timeout_seconds=min(spec.timeout_seconds, self._call_timeout),
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
            timeout = self._call_timeout
            if item.spec.transport == "http":
                timeout = min(item.spec.timeout_seconds, self._call_timeout)
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
        """关闭全部连接器进程与 HTTP 连接。"""
        stdio, self._stdio = self._stdio, []
        http, self._http_clients = self._http_clients, []
        self._pending = []
        for closer in [run.close for run in stdio] + [client.close for client in http]:
            try:
                await closer()
            except Exception:
                logger.exception("关闭连接器失败")


__all__ = [
    "DEFAULT_BASE_ENV",
    "ConnectorCallError",
    "ConnectorGate",
    "ConnectorSet",
    "ConnectorTool",
    "render_result",
    "truncate",
]
