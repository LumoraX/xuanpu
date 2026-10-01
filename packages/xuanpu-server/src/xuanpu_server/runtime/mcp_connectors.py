"""MCP 类连接器：``stdio`` 与 ``remote`` 两种传输的启动、列工具与调用。

- ``stdio``：平台启动业务包声明的 MCP server 进程。只用内核稳定层的 ``McpStdioClient``；
  进程经执行器起在这个运行时的连接器沙盒里（只读业务包目录、只写连接器自己的工作目录、
  可出网，见 ``sandbox.py``），不会退回到在平台节点上直接起。
- ``remote``：部署在别处、经 MCP Streamable HTTP 访问的 MCP server（``remote_mcp.py``）。
  不进沙盒，它本来就是一次网络调用。

两者共用一套启动流程（``open_mcp``）：建连、列工具、核对清单声明的工具都在、按白名单筛出
工具。失败统一归为运行时不可用：返回的提示只带异常类型（异常文本可能含有凭据），日志里记
按值脱敏后的排障线索。闸门与分派在 ``connectors.py``。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import taifeng

from xuanpu_server.errors import PackageInvalidError, PlatformError, RuntimeUnavailableError
from xuanpu_server.redaction import redact, tail
from xuanpu_server.runtime.http_api import ConnectorCallError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from xuanpu_spec.manifest import ConnectorSpec

    from xuanpu_server.runtime.connector_process import ObservingExecutor
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

# 一次调用的实现：参数 → (给模型看的文本, 是否出错)
Invoke = Callable[[dict[str, Any]], Awaitable[tuple[str, bool]]]


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


@dataclass(frozen=True)
class McpTool:
    """MCP server 列出的、在白名单内的一个工具。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    invoke: Invoke


class McpRun(Protocol):
    """一个 MCP 类连接器的运行实例：排障线索、调用、关闭。建连由 ``open_mcp`` 的参数给出。"""

    name: str

    async def diagnostics(self, exc: BaseException | None, *, settle: bool = False) -> str:
        """脱敏后的排障线索。``settle`` 表示先等底层进程退出片刻（对没有进程的实现无意义）。"""
        ...

    def invoker(self, tool: str) -> Invoke:
        """调用某个工具的函数。须在建连成功之后调用。"""
        ...

    async def close(self) -> None:
        """关闭连接器；启动没走完的也要收拾干净。"""
        ...


async def _list_mcp_tools(
    connect: Callable[[], Awaitable[taifeng.McpClient]],
) -> dict[str, dict[str, Any]]:
    """建连并列出全部工具：工具名 → 元数据。"""
    client = await connect()
    return {str(tool["name"]): tool for tool in await client.list_tools() if "name" in tool}


async def open_mcp(
    run: McpRun,
    spec: ConnectorSpec,
    connect: Callable[[], Awaitable[taifeng.McpClient]],
    *,
    timeout: float,
) -> list[McpTool]:
    """建连、列工具、核对清单声明的工具都在，返回白名单内的工具。

    ``run`` 须在调用之前登记到收尾列表里：启动到一半失败（含超过启动时限被取消），
    已经起来的进程或连接都由那里的 ``close()`` 收拾。

    Raises:
        PlatformError: 平台侧的失败（例如建连途中密钥被删掉），原样上抛。
        RuntimeUnavailableError: 起不来、握手或列工具失败、超过启动时限，或 server 没有提供
            清单里声明的工具。
    """
    try:
        listed = await asyncio.wait_for(_list_mcp_tools(connect), timeout=timeout)
    except PlatformError:
        raise
    except Exception as exc:
        # 外部 server 的失败方式不可穷举（起不来、握手超时、协议版本不符、列不出工具……），
        # 统一归为运行时不可用。返回的提示只带异常类型：异常文本可能含有注入进去的凭据。
        # 日志里记按值脱敏后的排障线索；不带原异常（from None），免得别处打印堆栈时
        # 把未脱敏的异常文本带出去
        logger.error("连接器 %s 启动失败：%s", run.name, await run.diagnostics(exc, settle=True))
        raise RuntimeUnavailableError(
            f"连接器 {spec.name!r} 启动失败：{type(exc).__name__}"
        ) from None
    missing = [tool.name for tool in spec.tools if tool.name not in listed]
    if missing:
        logger.error(
            "连接器 %s 缺少声明的工具 %s：%s",
            run.name,
            ", ".join(missing),
            await run.diagnostics(None),
        )
        raise RuntimeUnavailableError(
            f"连接器 {spec.name!r} 没有提供清单里声明的工具：{', '.join(missing)}"
        )
    found: list[McpTool] = []
    for name, meta in listed.items():
        if not spec.allows(name):
            continue
        schema = meta.get("inputSchema")
        found.append(
            McpTool(
                name=name,
                description=str(meta.get("description") or f"{spec.name} / {name}"),
                input_schema=schema if isinstance(schema, dict) else {"type": "object"},
                invoke=run.invoker(name),
            )
        )
    return found


async def stdio_environment(
    spec: ConnectorSpec, *, base_env: Mapping[str, str], tenant_id: str, vault: SecretVault
) -> tuple[dict[str, str], tuple[str, ...]]:
    """stdio 连接器进程的完整环境：基础环境加清单声明的变量，密钥在此解析成明文。

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
    env = dict(base_env)
    secrets: list[str] = []
    for name, item in spec.env.items():
        if item.secret is not None:
            env[name] = await vault.get(tenant_id, item.secret)
            secrets.append(env[name])
        else:
            assert item.value is not None
            env[name] = item.value
    return env, tuple(secrets)


@dataclass
class StdioRun:
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

    async def spawn(
        self, command: Sequence[str], *, env: dict[str, str], cwd: str, request_timeout: float
    ) -> taifeng.McpStdioClient:
        """起进程、握手。握手一成功就把客户端记下来，之后的失败由 ``close()`` 关掉它。"""
        self.client = await taifeng.McpStdioClient.spawn(
            list(command),
            env=env,
            cwd=cwd,
            executor=self.observer,
            request_timeout_seconds=request_timeout,
        )
        return self.client

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


__all__ = [
    "Invoke",
    "McpRun",
    "McpTool",
    "StdioRun",
    "open_mcp",
    "render_result",
    "stdio_environment",
    "truncate",
]
