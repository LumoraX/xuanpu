"""三态透明 Transcript 读取器（本节点直读 → 跨节点签名热读 → ObjectStore 冷归档）与自排查服务（ADR 0026）。"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import json
import logging
import time
from typing import TYPE_CHECKING

import httpx

from server.core.errors import AuthenticationError, NotFoundError
from server.core.objectstore import ObjectNotFoundError
from server.engine.transcripts.models import (
    TranscriptIndexRecord,
    TranscriptMode,
    TranscriptSlice,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from server.core.objectstore import ObjectStore
    from server.engine.transcripts.store import TranscriptIndexStore

    RemoteFetcher = Callable[
        [str, str, str, TranscriptMode, int, int, str], Awaitable[TranscriptSlice]
    ]

logger = logging.getLogger(__name__)
TICKET_HEADER = "X-Xuanpu-Node-Ticket"
DEFAULT_TICKET_TTL_SECONDS = 60.0
MAX_SLICE_LINES = 500


class NodeTicketSigner:
    """集群内部跨节点只读热代理的短期 HMAC-SHA256 票据签名与验签器。"""

    def __init__(self, secret: bytes, *, now: Callable[[], float] = time.time) -> None:
        """注入集群共享签名密钥（长度不少于 16 字节）。"""
        if len(secret) < 16:
            raise ValueError("节点签名密钥长度至少 16 字节")
        self._secret = secret
        self._now = now

    def sign(
        self, *, tenant_id: str, node_id: str, span_id: str,
        ttl_seconds: float = DEFAULT_TICKET_TTL_SECONDS,
    ) -> str:
        """签发短期只读票据。"""
        exp = int(self._now() + ttl_seconds)
        msg = f"{tenant_id}|{node_id}|{span_id}|{exp}"
        sig = hmac.new(self._secret, msg.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{exp}.{sig}"

    def verify(self, ticket: str, *, tenant_id: str, node_id: str, span_id: str) -> None:
        """校验签名与过期时间；不合法抛 ``AuthenticationError``。"""
        exp_str, sep, sig = ticket.partition(".")
        if not sep or not exp_str.isdigit():
            raise AuthenticationError("无效的节点间访问票据")
        exp = int(exp_str)
        if exp < int(self._now()):
            raise AuthenticationError("节点间访问票据已过期")
        msg = f"{tenant_id}|{node_id}|{span_id}|{exp}"
        expected = hmac.new(self._secret, msg.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise AuthenticationError("节点间访问票据签名不匹配")


def _slice_jsonl_text(
    raw_text: str,
    *,
    trace_id: str,
    span_id: str,
    mode: TranscriptMode,
    source: str,
    start_line: int,
    end_line: int,
) -> TranscriptSlice:
    """从 JSONL 全文中切出 1-based 闭区间 ``[start_line, end_line]``。"""
    all_lines = [line for line in raw_text.splitlines() if line.strip()]
    total = len(all_lines)
    if total == 0 or start_line > total:
        return TranscriptSlice(
            trace_id=trace_id, span_id=span_id, mode=mode, source=source,  # type: ignore[arg-type]
            start_line=start_line, end_line=0, total_lines=total, lines=(),
        )
    clamped_start = max(1, start_line)
    clamped_end = min(total, max(clamped_start, end_line), clamped_start + MAX_SLICE_LINES - 1)
    selected = tuple(json.loads(line) for line in all_lines[clamped_start - 1 : clamped_end])
    return TranscriptSlice(
        trace_id=trace_id, span_id=span_id, mode=mode, source=source,  # type: ignore[arg-type]
        start_line=clamped_start, end_line=clamped_end, total_lines=total, lines=selected,
    )


def _safe_local_file(data_dir: Path, relpath: str, mode: TranscriptMode) -> Path:
    """校验相对路径严格位于 ``<data_dir>/transcripts`` 下，防止路径穿越。"""
    root = (data_dir / "transcripts").resolve()
    candidate = (root / f"{relpath}.{mode}.jsonl").resolve()
    if root not in candidate.parents:
        raise NotFoundError("Transcript 路径非法")
    return candidate


async def _default_remote_fetch(
    node_addr: str,
    tenant_id: str,
    span_id: str,
    mode: TranscriptMode,
    start_line: int,
    end_line: int,
    ticket: str,
) -> TranscriptSlice:
    """经 HTTP 向目标活跃节点热拉取 Transcript 切片。"""
    base = node_addr.rstrip("/")
    url = f"{base}/internal/nodes/local/transcripts/{span_id}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            url,
            params={
                "tenant_id": tenant_id,
                "mode": mode,
                "start_line": start_line,
                "end_line": end_line,
            },
            headers={TICKET_HEADER: ticket},
        )
    if resp.status_code != 200:
        raise NotFoundError(f"远端节点热读失败：{resp.status_code}")
    data = resp.json()
    data["source"] = "remote_node"
    return TranscriptSlice.model_validate(data)


class TranscriptReader:
    """三态透明读取器：本节点直读 → 远端活跃节点签名热读 → ObjectStore 冷归档。"""

    def __init__(
        self,
        *,
        data_dir: Path,
        node_id: str,
        store: TranscriptIndexStore,
        objects: ObjectStore,
        signer: NodeTicketSigner,
        remote_fetch: RemoteFetcher | None = None,
    ) -> None:
        """依赖由平台注入；``remote_fetch`` 可在单测中替换为桩。"""
        self._data_dir = data_dir
        self._node_id = node_id
        self._store = store
        self._objects = objects
        self._signer = signer
        self._remote_fetch = remote_fetch or _default_remote_fetch

    async def read_local_only(
        self,
        tenant_id: str,
        span_id: str,
        *,
        mode: TranscriptMode = "compact",
        start_line: int = 1,
        end_line: int = 200,
    ) -> TranscriptSlice:
        """仅读本节点磁盘（供内部跨节点代理路由调用，禁止循环转发）。"""
        record = await self._store.get(tenant_id, span_id)
        if record.node_id != self._node_id:
            raise NotFoundError("该 Transcript 不在本节点本地热盘")
        path = _safe_local_file(self._data_dir, record.local_relpath, mode)
        if not path.is_file():
            raise NotFoundError("本地 Transcript 文件不存在")
        raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return _slice_jsonl_text(
            raw, trace_id=record.trace_id, span_id=span_id, mode=mode,
            source="local", start_line=start_line, end_line=end_line,
        )

    async def read(
        self,
        tenant_id: str,
        span_id: str,
        *,
        user_id: str | None = None,
        mode: TranscriptMode = "compact",
        start_line: int = 1,
        end_line: int = 200,
    ) -> TranscriptSlice:
        """集群内任意节点调用：按「本地直读 → 远端热读 → 对象存储冷读」自动透明解析。"""
        record = await self._store.get(tenant_id, span_id)
        if user_id is not None and record.user_id != user_id:
            raise NotFoundError("Transcript 索引不存在")
        # 1. 本节点命中直接读本地热盘
        if record.node_id == self._node_id:
            path = _safe_local_file(self._data_dir, record.local_relpath, mode)
            if path.is_file():
                raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
                return _slice_jsonl_text(
                    raw, trace_id=record.trace_id, span_id=span_id, mode=mode,
                    source="local", start_line=start_line, end_line=end_line,
                )
        # 2. 远端活跃节点热读（带短期签名票据）
        if record.node_id != self._node_id and record.node_addr:
            ticket = self._signer.sign(
                tenant_id=tenant_id, node_id=record.node_id, span_id=span_id
            )
            try:
                return await self._remote_fetch(
                    record.node_addr, tenant_id, span_id, mode, start_line, end_line, ticket
                )
            except Exception:
                logger.warning("跨节点热读 %s@%s 失败，尝试回退 ObjectStore 冷归档",
                               span_id, record.node_id)
        # 3. 共享 ObjectStore 冷归档回读
        obj_key = record.compact_object_key if mode == "compact" else record.full_object_key
        if obj_key is None:
            raise NotFoundError("Transcript 热盘不可达且尚未冷归档")
        try:
            compressed = await self._objects.get(obj_key)
        except ObjectNotFoundError as exc:
            raise NotFoundError("Transcript 归档对象不存在") from exc
        raw = await asyncio.to_thread(lambda: gzip.decompress(compressed).decode("utf-8"))
        return _slice_jsonl_text(
            raw, trace_id=record.trace_id, span_id=span_id, mode=mode,
            source="object_store", start_line=start_line, end_line=end_line,
        )

    async def list_for_trace(
        self, tenant_id: str, trace_id: str, *, user_id: str | None = None,
    ) -> tuple[TranscriptIndexRecord, ...]:
        """列出一条 trace 下的全部跨节点 Transcript 元数据。"""
        return await self._store.list_for_trace(tenant_id, trace_id, user_id=user_id)

    async def list_failed_traces(
        self, tenant_id: str, *, user_id: str | None = None, limit: int = 20,
    ) -> tuple[TranscriptIndexRecord, ...]:
        """面向自排查（Self-Diagnosis）的近期异常执行索引查询。"""
        return await self._store.list_failed(tenant_id, user_id=user_id, limit=limit)
