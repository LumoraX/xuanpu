"""平台与进程外服务共用的请求摘要规范，仅依赖标准库。"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

CONTEXT_HEADER = "X-Xuanpu-Context"


def request_fingerprint(
    method: str,
    path: str,
    query: Sequence[tuple[str, str]],
    body: Any,
    idempotency_key: str = "",
    content_type: str = "application/json",
) -> str:
    """规范化方法、路径、查询、JSON 与关键请求头后计算 SHA256；拒绝非有限数。"""
    # 仅按键稳定排序；同名值原顺序可能影响服务端取首项或末项的语义。
    canonical = json.dumps(
        {
            "method": method.upper(),
            "path": path,
            "query": sorted(query, key=lambda pair: pair[0]),
            "body": body,
            "idempotency_key": idempotency_key,
            "content_type": content_type,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
