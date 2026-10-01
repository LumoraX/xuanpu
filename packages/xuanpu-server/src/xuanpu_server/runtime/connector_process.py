"""stdio 连接器进程的排障线索，以及内核 MCP 日志的卫生处理。

- ``ObservingExecutor``：每个 stdio 连接器起进程时包一层，记下进程本身与它 stderr 的末尾。
  内核照常读走 stderr（不读的话进程写满管道会卡死），读过的末尾在这里留一份。启动失败时
  内核已经把客户端关掉了，拿不到 ``stderr_tail``，退出码与 stderr 只能从这里取。
- ``install_kernel_mcp_log_guard``：内核 MCP 客户端会把连接器写到 stdout 上的非 JSON 行原样、
  不限长度写进日志（``mcp: invalid JSON: %r``）。那是连接器的原始输出，可能带着注入给它的
  凭据；而内核的日志记录里不知道是哪个连接器、该用哪些密钥脱敏，按值脱敏做不到。所以给
  这几个 logger 挂过滤器：字节串参数（连接器的原始输出）一律不记内容、只记长度；其余参数
  的表示超过 200 字符的截断并标注。远端 MCP 连接器用的 HTTP 客户端（``taifeng.mcp.http_client``）
  也在其列：它会把远端回的、解析不了的 SSE 数据截取前 200 字符写进日志。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import taifeng

if TYPE_CHECKING:
    from collections.abc import Mapping

# 每个连接器进程的 stderr 在这里留多少字节（脱敏之后再截到要记的长度）
_STDERR_KEEP_BYTES = 8 * 1024
# 强杀之后等进程退出的时限
_KILL_WAIT_SECONDS = 5.0

_KERNEL_MCP_LOGGERS = (
    "taifeng.mcp.stdio_client",
    "taifeng.mcp.http_client",
    "taifeng.mcp.server_messages",
    "taifeng.mcp.bridge",
    "taifeng.mcp.cancellation",
)
_MAX_LOGGED_ARG_CHARS = 200

logger = logging.getLogger(__name__)


class _StderrRecorder:
    """包住进程的 stderr 读取端：读到的内容原样交给读的一方，末尾在这里留一段。"""

    def __init__(self, stream: taifeng.CommandOutput) -> None:
        """包住 ``stream``。"""
        self._stream = stream
        self._kept = bytearray()
        self._dropped = False

    async def read(self, n: int = -1) -> bytes:
        """读至多 ``n`` 字节。"""
        return self._keep(await self._stream.read(n))

    async def readline(self) -> bytes:
        """读一行。"""
        return self._keep(await self._stream.readline())

    def _keep(self, chunk: bytes) -> bytes:
        """留下读到的内容，超出上限就丢掉最早的部分。"""
        self._kept += chunk
        overflow = len(self._kept) - _STDERR_KEEP_BYTES
        if overflow > 0:
            del self._kept[:overflow]
            self._dropped = True
        return chunk

    def text(self) -> str:
        """留下的内容（按 UTF-8 解码，坏字节替换）。

        丢掉过前面的内容时，第一行是从中间截断的，连同它一起去掉：截断处可能正好切开一个
        密钥，剩下的半截没法按值脱敏。
        """
        text = self._kept.decode("utf-8", errors="replace")
        if self._dropped:
            _, _, text = text.partition("\n")
        return text


class ObservedProcess:
    """连接器进程的代理：stderr 经记录器读出，其余原样转给真正的进程。

    满足内核的 ``StreamingCommandProcess`` 协议。
    """

    def __init__(self, process: taifeng.StreamingCommandProcess) -> None:
        """包住 ``process``。"""
        self._process = process
        self._stderr = None if process.stderr is None else _StderrRecorder(process.stderr)

    @property
    def stdin(self) -> taifeng.CommandInput | None:
        """标准输入的写入端。"""
        return self._process.stdin

    @property
    def stdout(self) -> taifeng.CommandOutput | None:
        """标准输出的读取端。"""
        return self._process.stdout

    @property
    def stderr(self) -> taifeng.CommandOutput | None:
        """标准错误的读取端（经记录器）。"""
        return self._stderr

    @property
    def returncode(self) -> int | None:
        """退出码；未结束为 None。"""
        return self._process.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        """读完 stdout / stderr 并等待退出。"""
        return await self._process.communicate()

    def kill(self) -> None:
        """强制终止（连同它派生的进程）。"""
        self._process.kill()

    async def wait(self) -> int:
        """等待退出，返回退出码。"""
        return await self._process.wait()

    def stderr_text(self) -> str:
        """读过的 stderr 末尾（未脱敏）。"""
        return "" if self._stderr is None else self._stderr.text()


class ObservingExecutor:
    """一个 stdio 连接器专用的执行器：经真正的执行器起进程，并记下这个进程。

    Attributes:
        process: 起来的进程；还没起时为 None。
    """

    def __init__(self, inner: taifeng.CommandExecutor) -> None:
        """包住 ``inner``。"""
        self._inner = inner
        self.process: ObservedProcess | None = None

    async def start(self, spec: taifeng.CommandSpec) -> taifeng.CommandProcess:
        """起进程。不是流式进程的原样交回，由内核报错并收拾。"""
        process = await self._inner.start(spec)
        if not isinstance(process, taifeng.StreamingCommandProcess):
            return process
        self.process = ObservedProcess(process)
        return self.process

    async def kill(self) -> None:
        """强杀起过的进程并等它退出；没起过进程时什么都不做。"""
        if self.process is None:
            return
        self.process.kill()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=_KILL_WAIT_SECONDS)
        except TimeoutError:
            logger.error("连接器进程强杀后 %s 秒仍未退出", _KILL_WAIT_SECONDS)


class _Clipped:
    """日志参数的替身：``%s`` 与 ``%r`` 都输出给定的文本。"""

    def __init__(self, text: str) -> None:
        self._text = text

    def __str__(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return self._text


def _scrub(value: Any) -> Any:
    """一个日志参数：字节串不记内容；表示太长的截断并标注；其余原样。"""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, bytes | bytearray):
        return _Clipped(f"<连接器输出 {len(value)} 字节，内容不记>")
    shown = repr(value)
    if len(shown) <= _MAX_LOGGED_ARG_CHARS:
        return value
    return _Clipped(f"{shown[:_MAX_LOGGED_ARG_CHARS]}…[已截断，原长 {len(shown)} 字符]")


class KernelMcpLogGuard(logging.Filter):
    """内核 MCP 日志的过滤器：见模块说明。只改写参数，记录照常放行。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """改写这条记录的参数。"""
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(_scrub(item) for item in args)
        elif args:
            mapping: Mapping[str, Any] = args
            record.args = {key: _scrub(item) for key, item in mapping.items()}
        return True


def install_kernel_mcp_log_guard() -> None:
    """给内核 MCP 的几个 logger 挂上过滤器。重复调用只挂一次。

    logger 上的过滤器只作用于直接发到这个 logger 的记录，所以逐个挂在内核实际打日志的
    模块 logger 上；内核以后新增的 MCP 模块要在 ``_KERNEL_MCP_LOGGERS`` 里补上。
    """
    for name in _KERNEL_MCP_LOGGERS:
        target = logging.getLogger(name)
        if not any(isinstance(item, KernelMcpLogGuard) for item in target.filters):
            target.addFilter(KernelMcpLogGuard())


__all__ = [
    "KernelMcpLogGuard",
    "ObservedProcess",
    "ObservingExecutor",
    "install_kernel_mcp_log_guard",
]
