"""连接器：把业务包声明的 MCP server 接成模型可用的工具。

连接器是业务方扩展平台能力的通道（ADR 0002），也是效应面的一个闸门（ADR 0004 决策 3）。
每次调用依次经过：白名单 → 审批 → 调用 → 审计。

凭据注入：业务包里只写密钥名；启动连接器进程时从保管库取出明文，放进它的环境变量。
进程的环境只有清单里声明的变量加上平台给的最小集合，不继承平台进程自己的环境。

第一版只用内核稳定层的 ``McpStdioClient`` 自行包装工具；连接器进程跑在平台节点上
（见 ``docs/architecture/kernel-seam-backlog.md`` 第 6、7 项）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import taifeng
from xuanpu_spec import connector_tool_name

from xuanpu_server.audit import digest_arguments
from xuanpu_server.errors import RuntimeUnavailableError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from xuanpu_spec.manifest import ConnectorSpec, Effect

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.runtime.events import SubmissionIndex
    from xuanpu_server.secrets import SecretVault

logger = logging.getLogger(__name__)

# 连接器进程的最小环境：只够找到解释器
DEFAULT_BASE_ENV: Mapping[str, str] = {
    "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    "LANG": "C.UTF-8",
}

_MAX_OUTPUT_CHARS = 64 * 1024

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
    text = "\n".join(parts)
    if len(text) > _MAX_OUTPUT_CHARS:
        text = text[:_MAX_OUTPUT_CHARS] + f"\n…[共 {len(text)} 字符，已截断]"
    return text, bool(result.get("isError"))


class ConnectorGate:
    """一个连接器工具的闸门：审批、调用、审计。"""

    def __init__(
        self,
        *,
        client: taifeng.McpStdioClient,
        connector: str,
        tool: str,
        effect: Effect,
        tenant_id: str,
        policy: taifeng.PermissionPolicy,
        audit: AuditLog,
        submissions: SubmissionIndex,
        timeout_seconds: float,
    ) -> None:
        """绑定一个工具。"""
        self._submissions = submissions
        self._client = client
        self._connector = connector
        self._tool = tool
        self._effect = effect
        self._tenant_id = tenant_id
        self._policy = policy
        self._audit = audit
        self._timeout = timeout_seconds
        self.full_name = connector_tool_name(connector, tool)

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
                reason=f"调用连接器 {self._connector} 的工具 {self._tool}（{self._effect}）",
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
            result = await asyncio.wait_for(
                self._client.call_tool(self._tool, args), timeout=self._timeout
            )
        except TimeoutError:
            await self._record(
                ctx, args, decision="allowed", started=started, is_error=True, detail="timeout"
            )
            return taifeng.ToolResult.error("connector_timeout", reason="timeout")
        except taifeng.McpToolError as exc:
            await self._record(
                ctx, args, decision="allowed", started=started, is_error=True, detail=str(exc)
            )
            return taifeng.ToolResult.error(f"connector_error: {exc}", reason="connector_error")
        text, is_error = render_result(result)
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
            base_env: 连接器进程的基础环境。
            call_timeout_seconds: 单次工具调用的超时。
            startup_timeout_seconds: 启动连接器并列出工具的超时。
        """
        self._tenant_id = tenant_id
        self._vault = vault
        self._audit = audit
        self._submissions = submissions
        self._base_env = dict(base_env)
        self._call_timeout = call_timeout_seconds
        self._startup_timeout = startup_timeout_seconds
        self._clients: list[taifeng.McpStdioClient] = []
        self._pending: list[tuple[ConnectorSpec, taifeng.McpStdioClient, dict[str, Any]]] = []

    async def _environment(self, spec: ConnectorSpec) -> dict[str, str]:
        """连接器进程的完整环境：基础环境加清单声明的变量，密钥在此解析成明文。"""
        env = dict(self._base_env)
        for name, item in spec.env.items():
            if item.secret is not None:
                env[name] = await self._vault.get(self._tenant_id, item.secret)
            else:
                assert item.value is not None
                env[name] = item.value
        return env

    async def start(self, spec: ConnectorSpec, *, cwd: str) -> dict[str, Effect]:
        """启动一个连接器并列出它的工具，返回「完整工具名 → 副作用分类」。

        此时还不生成工具：工具的处理函数要用到权限策略，而策略要等所有连接器的工具
        都列出来之后才能生成。

        Raises:
            SecretMissingError: 引用的密钥尚未配置。
            RuntimeUnavailableError: 连接器起不来，或没有提供清单里声明的工具。
        """
        env = await self._environment(spec)
        try:
            client = await asyncio.wait_for(
                taifeng.McpStdioClient.spawn(
                    list(spec.command), env=env, cwd=cwd, request_timeout_seconds=self._call_timeout
                ),
                timeout=self._startup_timeout,
            )
        except Exception as exc:
            # 外部进程的失败方式不可穷举（起不来、握手超时、协议版本不符……），统一归为
            # 运行时不可用。提示里只带异常类型：异常文本可能含有注入进去的凭据
            logger.exception("连接器 %s 启动失败", spec.name)
            raise RuntimeUnavailableError(
                f"连接器 {spec.name!r} 启动失败：{type(exc).__name__}"
            ) from exc
        self._clients.append(client)
        listed = {str(tool["name"]): tool for tool in await client.list_tools() if "name" in tool}
        missing = [tool.name for tool in spec.tools if tool.name not in listed]
        if missing:
            raise RuntimeUnavailableError(
                f"连接器 {spec.name!r} 没有提供清单里声明的工具：{', '.join(missing)}"
            )
        effects: dict[str, Effect] = {}
        for name, meta in listed.items():
            if spec.allows(name):
                effects[connector_tool_name(spec.name, name)] = spec.effect_of(name)
                self._pending.append((spec, client, meta))
        return effects

    def build_tools(self, policy: taifeng.PermissionPolicy) -> list[ConnectorTool]:
        """用最终的权限策略生成全部工具。"""
        tools: list[ConnectorTool] = []
        for spec, client, meta in self._pending:
            name = str(meta["name"])
            effect = spec.effect_of(name)
            gate = ConnectorGate(
                client=client,
                connector=spec.name,
                tool=name,
                effect=effect,
                tenant_id=self._tenant_id,
                policy=policy,
                audit=self._audit,
                submissions=self._submissions,
                timeout_seconds=self._call_timeout,
            )
            schema = meta.get("inputSchema")
            tools.append(
                ConnectorTool(
                    connector=spec.name,
                    tool=name,
                    effect=effect,
                    spec=taifeng.ToolSpec(
                        name=gate.full_name,
                        description=str(meta.get("description") or f"{spec.name} / {name}"),
                        input_schema=schema if isinstance(schema, dict) else {"type": "object"},
                        handler=gate,
                        parallel_safe=effect == "read",
                        # 内核自己的超时留出余量，让闸门先超时并写下审计
                        timeout_seconds=self._call_timeout + 5.0,
                        effect_kind=_EFFECT_KIND[effect],
                        can_suspend=True,
                    ),
                )
            )
        return tools

    async def close(self) -> None:
        """关闭全部连接器进程。"""
        clients, self._clients, self._pending = self._clients, [], []
        for client in clients:
            try:
                await client.close()
            except Exception:
                logger.exception("关闭连接器进程失败")


__all__ = [
    "DEFAULT_BASE_ENV",
    "ConnectorGate",
    "ConnectorSet",
    "ConnectorTool",
    "render_result",
]
