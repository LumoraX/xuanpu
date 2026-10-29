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
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import taifeng
from xuanpu_spec import OpenApiError, load_openapi

from xuanpu_server.audit import digest_arguments
from xuanpu_server.errors import PackageInvalidError, PlatformError, RuntimeUnavailableError
from xuanpu_server.runtime.call_context import bind_call_context
from xuanpu_server.runtime.connector_process import ObservingExecutor
from xuanpu_server.runtime.gate_audit import CANCELLED, ConnectorToolInfo, gate_call_data
from xuanpu_server.runtime.http_api import ConnectorCallError, HttpApiClient, resolve_base_url
from xuanpu_server.runtime.mcp_connectors import (
    StdioRun,
    open_mcp,
    stdio_environment,
    truncate,
)
from xuanpu_server.runtime.remote_mcp import RemoteRun
from xuanpu_server.runtime.sandbox import DEFAULT_BASE_ENV

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path

    from xuanpu_spec.manifest import ConnectorSpec, Effect

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.connections.managed_http import ManagedHttpTools
    from xuanpu_server.runtime.call_context import CallContextSigner
    from xuanpu_server.runtime.events import SubmissionIndex
    from xuanpu_server.runtime.mcp_connectors import Invoke, McpRun, McpTool
    from xuanpu_server.secrets import SecretVault

    from .effect_receipts import EffectReceipt
    from .effect_store import EffectStore

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
    invoke_context: Callable[[dict[str,Any],taifeng.ToolContext],Awaitable[tuple[str,bool]]] | None = None

    @property
    def full_name(self) -> str:
        """模型看到的工具名。"""
        return self.spec.tool_name(self.tool)

    @property
    def info(self) -> ConnectorToolInfo:
        """登记信息：属于哪个连接器、走什么传输、副作用分类。"""
        return ConnectorToolInfo(
            connector=self.spec.name,
            transport=self.spec.transport,
            tool=self.tool,
            effect=self.effect,
        )


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
        info: ConnectorToolInfo,
        full_name: str,
        tenant_id: str,
        policy: taifeng.PermissionPolicy,
        audit: AuditLog,
        submissions: SubmissionIndex,
        timeout_seconds: float,
        invoke_context: Callable[[dict[str,Any],taifeng.ToolContext],Awaitable[tuple[str,bool]]] | None = None,
    ) -> None:
        """绑定一个工具。"""
        from xuanpu_server.work_tasks.execution_context import current_execution

        self._task_execution = current_execution.get()
        self._read_receipts: set[str] = set()
        self._invoke_context=invoke_context
        self._invoke = invoke
        self._info = info
        self._tenant_id = tenant_id
        self._policy = policy
        self._audit = audit
        self._submissions = submissions
        self._timeout = timeout_seconds
        self.full_name = full_name
        # 调用途中被取消时在后台写的过闸记录：持有引用，写完之前不被回收
        self._cancel_writes: set[asyncio.Task[None]] = set()

    async def _record(
        self,
        ctx: taifeng.ToolContext,
        arguments_digest: str,
        *,
        decision: str,
        started: float,
        is_error: bool,
        detail: str = "",
    ) -> None:
        """写一条过闸记录。参数只记摘要。

        归属的那一轮取 ``ctx.extras["submission_id"]``：审批通过后的重跑里，内核放的是触发
        重跑的那次送回的提交标识；内核自己发起的重跑（如到期裁决）没有这个键，记为 None，
        会话按线程归属。``user_id`` 是会话主人，与会话一起从对照表里查到。
        """
        extra = ctx.extras.get("submission_id")
        submission_id = extra if isinstance(extra, str) else None
        session = self._submissions.session_of(submission_id, ctx.thread_id)
        try:
            await self._audit.gate(
                self._tenant_id,
                "connector_call",
                gate_call_data(
                    self._info,
                    decision=decision,
                    is_error=is_error,
                    detail=detail,
                    call_id=ctx.call_id,
                    thread_id=ctx.thread_id,
                    arguments_digest=arguments_digest,
                    submission_id=submission_id,
                    duration_ms=int((time.monotonic() - started) * 1000),
                ),
                session_id=session.session_id,
                user_id=session.user_id,
            )
        except Exception:
            if decision == "allowed" and self._info.effect != "read":
                # 外部写入已发出：审计故障也必须穿透为挂起，不能被 runtime 转成可重试错误。
                logger.exception("写连接器的结果审计未保存，暂停等待核实")
                self._error("connector_audit_unavailable", "audit_unavailable", ctx, arguments_digest)
            raise

    async def _record_cancelled(
        self, ctx: taifeng.ToolContext, arguments_digest: str, started: float
    ) -> None:
        """调用途中被取消时的过闸记录（``detail="cancelled"``）：尽力写，写不进去只记日志。

        写入放在单独的任务里、经 ``asyncio.shield`` 等它：等的时候再被取消一次，这里照样以
        取消结束，写入不被打断，在后台写完。任务登记在 ``_cancel_writes`` 里，写完之前不会被
        回收。
        """

        async def write() -> None:
            """写记录；失败只记日志（这时正在传取消，不能换成写入的错误）。"""
            try:
                await self._record(
                    ctx,
                    arguments_digest,
                    decision="allowed",
                    started=started,
                    is_error=True,
                    detail=CANCELLED,
                )
            except Exception:
                logger.exception(
                    "连接器 %s 的工具 %s 调用途中被取消，过闸记录没有写进去",
                    self._info.connector,
                    self._info.tool,
                )

        task = asyncio.ensure_future(write())
        self._cancel_writes.add(task)
        task.add_done_callback(self._cancel_writes.discard)
        await asyncio.shield(task)

    def _error(self, text: str, reason: str, ctx: taifeng.ToolContext, digest: str) -> taifeng.ToolResult:
        """写调用结果未确认时挂起，不能把可重试错误直接送回模型。"""
        if self._info.effect == "read":
            return taifeng.ToolResult.error(text, reason=reason)
        raise taifeng.SuspendSignal(taifeng.PendingRequest(
            request_id="unknown_" + secrets.token_hex(12),
            reason=taifeng.SuspendReason.TOOL_OUTCOME_UNKNOWN,
            related_call_id=ctx.call_id,
            payload_schema={"type": "object", "properties": {
                "action": {"type": "string", "enum": ["provide", "abort"]},
                "output": {"type": "string"}, "is_error": {"type": "boolean"}},
                "required": ["action"]},
            detail={"connector": self._info.connector, "tool": self._info.tool,
                    "kind": reason, "arguments_digest": digest, "call_id": ctx.call_id,
                    "thread_id": ctx.thread_id},
        ))

    async def _task_intent(self, ctx: taifeng.ToolContext, digest: str) -> tuple[EffectStore | None, EffectReceipt | None]:
        """工具网络派发前先完成持久写屏障。"""
        info = self._info
        receipt = None
        effect_store = None
        if self._task_execution is not None and info.effect != 'read':
            from xuanpu_server.runtime.effect_store import EffectStore

            effect_store = EffectStore(self._task_execution)
            receipt = await effect_store.begin(call_id=ctx.call_id, tool=self.full_name,
                arguments_digest=digest.removeprefix("sha256:"), submission_id=ctx.extras.get('submission_id'))
        return effect_store,receipt

    async def _task_receipt(self, ctx: taifeng.ToolContext, digest: str, text: str,
                            effect_store: EffectStore | None, receipt: EffectReceipt | None) -> None:
        """真实只读回执或权威写回执先于模型继续；默认不确认外部写入。"""
        info = self._info
        if effect_store is not None and receipt is not None:
            try:
                verifier = effect_store.binding.store.effect_verifiers.get(self.full_name)
                if verifier is None:
                    self._error('external_receipt_requires_authority', 'authority_unverified', ctx, digest)
                    return
                proof = await verifier.verify_response(receipt, text)
                if proof.state != 'confirmed_success':
                    self._error('external_receipt_not_confirmed', 'authority_unverified', ctx, digest)
                    return
                await effect_store.confirm(receipt, text, authority=proof.authority)
            except asyncio.CancelledError:
                raise
            except taifeng.SuspendSignal:
                raise
            except Exception:
                self._error('external_receipt_not_saved','authority_unverified',ctx,digest)
                return
        if self._task_execution is not None and info.effect == 'read':
            from xuanpu_server.tenancy import Principal
            from xuanpu_server.work_tasks.store import require_task, save_record

            binding, lease = self._task_execution, self._task_execution.lease
            who = Principal(role='tenant_member', tenant_id=lease.tenant_id, user_id=lease.user_id, key_id=None)
            async with binding.store.workspace.transaction(who, 'work-task:exec:' + lease.task_id) as state:
                task = require_task(state, lease.task_id)
                receipts = list(task.result_receipt.get('read_receipts', []))
                if len(receipts) >= 256:
                    raise RuntimeUnavailableError('能力工具回执超过上限')
                import hashlib

                receipts.append({'call_id': ctx.call_id, 'tool': self.full_name,
                    'attempt_no':lease.attempt_no,'epoch':lease.epoch,'session_id':task.session_id,
                    'arguments_digest': digest, 'result_digest': hashlib.sha256(text.encode()).hexdigest(),
                    'submission_id': ctx.extras.get('submission_id')})
                save_record(state, task.model_copy(update={'result_receipt': {'read_receipts': receipts},
                    'state_version': task.state_version + 1, 'updated_at': max(binding.now(), task.updated_at)}),
                    'read_receipt_saved')

    async def __call__(self, args: dict[str, Any], ctx: taifeng.ToolContext) -> taifeng.ToolResult:
        """工具处理函数。"""
        started = time.monotonic()
        info = self._info
        # 摘要只算一次：过闸记录用它；也放进审批请求（进待办的 detail），人工拒绝的补记
        # 拿到的就是同一个值
        digest = digest_arguments(args)
        # 审批：需要人来定时，这里抛出挂起信号，本轮对话停在这一步；
        # 审批通过后内核带着同一个 call_id 重新进来，这次直接放行
        decision = await self._policy.check(
            taifeng.PermissionRequest(
                scope="tool_use",
                target=self.full_name,
                reason=f"调用连接器 {info.connector} 的 {info.tool}（{info.effect}）",
                metadata={
                    "call_id": ctx.call_id,
                    "thread_id": ctx.thread_id,
                    "args": args,
                    "arguments_digest": digest,
                },
            )
        )
        if not decision.granted:
            await self._record(
                ctx,
                digest,
                decision="denied",
                started=started,
                is_error=True,
                detail=decision.reason,
            )
            return taifeng.ToolResult.error(
                f"permission_denied: {decision.reason}", reason="permission_denied"
            )
        effect_store, receipt = await self._task_intent(ctx,digest)
        try:
            session = self._submissions.session_of(ctx.extras.get("submission_id"), ctx.thread_id)
            with bind_call_context(session, ctx.call_id):
                text, is_error = await asyncio.wait_for(
                    self._invoke(args) if self._invoke_context is None else self._invoke_context(args, ctx),
                    timeout=self._timeout,
                )
        except TimeoutError:
            await self._record(
                ctx, digest, decision="allowed", started=started, is_error=True, detail="timeout"
            )
            return self._error("connector_timeout", "timeout", ctx, digest)
        except (taifeng.McpToolError, ConnectorCallError) as exc:
            await self._record(
                ctx, digest, decision="allowed", started=started, is_error=True, detail=str(exc)
            )
            return self._error(f"connector_error: {exc}", "connector_error", ctx, digest)
        except PlatformError as exc:
            # 调用途中平台侧出了问题（比如密钥刚被删掉）：记下来，告诉模型这次调用没成
            await self._record(
                ctx, digest, decision="allowed", started=started, is_error=True, detail=exc.code
            )
            return self._error(f"{exc.code}: {exc.message}", exc.code, ctx, digest)
        except taifeng.SuspendSignal:
            # 挂起信号不是失败：照常穿透，由内核停下本轮
            raise
        except asyncio.CancelledError:
            # 调用已经发出、结果还没回来时整个任务被取消（关闭会话、租约丢失后强制收尾、停机
            # 等到期）：外部系统可能已经执行了，同样要留过闸记录（规则 1）。取消必须照常往上传
            await self._record_cancelled(ctx, digest, started)
            raise
        except Exception as exc:
            # 最后一道：调用里冒出了没预料到的异常。无论如何都要留下过闸记录（规则 1），
            # 而异常文本里是什么说不准（可能带着凭据或远端回的任意内容），所以过闸记录、
            # 日志与交给模型的错误都只写异常类型，不带原文
            kind = type(exc).__name__
            logger.error("连接器 %s 的工具 %s 调用时出了意外：%s", info.connector, info.tool, kind)
            await self._record(
                ctx, digest, decision="allowed", started=started, is_error=True, detail=kind
            )
            return self._error(f"connector_error: {kind}", "connector_error", ctx, digest)
        if not is_error:
            await self._task_receipt(ctx, digest, text, effect_store, receipt)
        await self._record(ctx, digest, decision="allowed", started=started, is_error=is_error)
        if is_error:
            return self._error(text or "connector_error", "tool_error", ctx, digest)
        return taifeng.ToolResult.ok(text, connector=info.connector, tool=info.tool)


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
        managed_connections: ManagedHttpTools | None = None,
        package_digest: str = "",
        strict_cleanup: bool = False,
        context_signer: CallContextSigner | None = None,
    ) -> None:
        """
        Args:
            tenant_id: 所属租户。
            vault: 密钥保管库。
            audit: 审计写入口。
            submissions: submission → 会话与主人的对照表，过闸记录据此归到会话与主人名下。
            executor: 在连接器沙盒里起 stdio 连接器进程的执行器；None 表示没有连接器沙盒，
                这时遇到 stdio 连接器直接报错，不在平台节点上起进程。
            base_env: 连接器进程的基础环境（由组装方给出，含指向连接器工作目录的
                ``HOME`` / ``TMPDIR`` / ``WORKSPACE``）；清单声明的变量与密钥加在它上面。
            call_timeout_seconds: 单次工具调用的超时（http 与 remote 连接器另受清单里的
                超时约束，取小的）。
            startup_timeout_seconds: 启动（或连上）连接器并列出工具的超时。
            context_signer: 平台部署配置允许的调用签名器；None 表示未启用。
        """
        self._managed_connections=managed_connections
        self._package_digest=package_digest
        self._tenant_id = tenant_id
        self._vault = vault
        self._audit = audit
        self._submissions = submissions
        self._executor = executor
        self._base_env = dict(base_env)
        self._call_timeout = call_timeout_seconds
        self._startup_timeout = startup_timeout_seconds
        self._context_signer = context_signer
        # stdio 与 remote 两种 MCP 连接器的运行实例；先登记再启动，启动到一半失败也归 close() 收拾
        self._mcp: list[McpRun] = []
        self._http_clients: list[HttpApiClient] = []
        self._pending: list[_Pending] = []
        self._observed: dict[str, dict[str, Any]] = {}
        self._strict_cleanup = strict_cleanup
        self._failed_closers: list[Callable[[], Awaitable[None]]] = []
        # 工具全名 → 登记信息。来自清单与列出的工具，关闭连接器之后仍然有效
        self._catalog: dict[str, ConnectorToolInfo] = {}

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
        self._catalog.update((item.full_name, item.info) for item in found)
        return {item.full_name: item.effect for item in found}

    def describe(self, full_name: str) -> ConnectorToolInfo | None:
        """按模型看到的工具名查它属于哪个连接器、走什么传输、副作用分类。

        不是这个运行时的连接器工具（内核工具、脚本、表单等）返回 None。stdio 与 remote 的
        工具名前缀相同，要知道传输只能查这里。
        """
        return self._catalog.get(full_name)

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
        return await open_mcp(run, spec, spawn, timeout=self._startup_timeout,
                              observe=functools.partial(self._observe, spec))

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
        return await open_mcp(run, spec, connect, timeout=self._startup_timeout,
                              observe=functools.partial(self._observe, spec))

    def _observe(self, spec: ConnectorSpec, tools: dict[str, dict[str, Any]]) -> None:
        """R4预检另外冻结白名单过滤前的完整工具结构，不赋予调用权。"""
        from xuanpu_server.capabilities.delivery import fingerprint

        for name, tool in tools.items():
            schema = tool.get('inputSchema')
            self._observed[spec.tool_name(name)] = {'name': spec.tool_name(name),
                'schema_sha256': fingerprint(schema if isinstance(schema, dict) else {'type': 'object'}),
                'effect': spec.effect_of(name)}

    def inventory_digest(self) -> str:
        """只返回不可变实际清单摘要，工具描述不进入配置或审计。"""
        from xuanpu_server.capabilities.delivery import fingerprint

        return fingerprint([self._observed[k] for k in sorted(self._observed)])

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
        base_url = resolve_base_url(spec, document)
        if spec.context_audience is not None:
            if self._context_signer is None:
                raise PackageInvalidError("连接器要求调用签名，但平台未配置 call_context")
            self._context_signer.require_audience(base_url, spec.context_audience)
        client = HttpApiClient(
            base_url=base_url,
            context_audience=spec.context_audience,
            context_signer=self._context_signer,
            auth=spec.auth,
            tenant_id=self._tenant_id,
            vault=self._vault,
            timeout_seconds=self._timeout_of(spec),
        )
        self._http_clients.append(client)

        def invoker(operation_id: str) -> Invoke:
            """一个 operation 的调用函数。"""
            operation = document.operations[operation_id]

            async def invoke(args: dict[str, Any]) -> tuple[str, bool]:
                """发一次请求，返回截断后的文本与是否出错。"""
                text, is_error = await client.call(operation, args)
                return truncate(text), is_error

            return invoke

        async def invoke_context(operation_id: str,args: dict[str,Any],ctx: taifeng.ToolContext) -> tuple[str,bool]:
            """当前绑定存在时强制进入员工broker，不退回静态认证。"""
            managed=self._managed_connections
            if managed is not None:
                binding=await managed.lookup(self._tenant_id,self._package_digest,spec.name,operation_id)
                if binding is not None:
                    return await managed.invoke(self._tenant_id,self._package_digest,spec,
                        document.operations[operation_id],resolve_base_url(spec,document),args,ctx)
            return await invoker(operation_id)(args)

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
                    invoke_context=functools.partial(invoke_context,operation_id),
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
                invoke_context=item.invoke_context,
                info=item.info,
                full_name=item.full_name,
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
        closers, self._failed_closers = self._failed_closers + [run.close for run in runs] + [client.close for client in http], []
        for index, closer in enumerate(closers):
            try:
                await closer()
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    self._failed_closers.extend(closers[index:])
                    raise
                self._failed_closers.append(closer)
                if self._strict_cleanup:
                    logger.warning("预检连接器关闭失败: %s", type(exc).__name__)
                else:
                    logger.exception("关闭连接器失败")
        if self._strict_cleanup and self._failed_closers:
            from xuanpu_server.errors import RuntimeCleanupRequiredError

            raise RuntimeCleanupRequiredError('预检连接器尚未退出', self.close)


__all__ = [
    "ConnectorCallError",
    "ConnectorGate",
    "ConnectorSet",
    "ConnectorTool",
    "truncate",
]
