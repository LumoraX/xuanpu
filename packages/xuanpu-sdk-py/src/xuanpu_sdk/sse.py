"""解析 Server-Sent Events。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_sdk.errors import XuanpuTransportError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """按行读入，产出 ``(事件名, 数据)``。注释行与心跳行跳过。"""
    name: str | None = None
    data: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\r\n")
        if line == "":
            if name is not None:
                joined = "\n".join(data)
                try:
                    payload = json.loads(joined) if joined else {}
                except ValueError as exc:
                    raise XuanpuTransportError(f"事件 {name!r} 的数据不是 JSON") from exc
                if not isinstance(payload, dict):
                    raise XuanpuTransportError(f"事件 {name!r} 的数据不是对象")
                yield name, payload
            name, data = None, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            name = value
        elif field == "data":
            data.append(value)


__all__ = ["parse_sse"]
