"""双层脱敏本地 JSONL Transcript 写入器与冷归档工具（ADR 0026）。

- 统一写到 ``<data_dir>/transcripts/<tenant_dir>/<yyyy-mm-dd>/<trace_id>/<span_id>.{compact,full}.jsonl``；
- 目录权限 ``0o700``、文件权限 ``0o600``；
- 入盘前强制经 ``sanitize`` + 凭据正则二次脱敏，绝不落盘密钥/Cookie/Authorization 明文；
- ``compact.jsonl`` 对超长字符串截断并登记 ``truncated_fields``，与 ``full.jsonl`` 严格行号对齐。
"""

from __future__ import annotations

import asyncio
import gzip
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from server.core.audit import sanitize
from server.core.objectstore import validate_key
from server.core.tenancy import tenant_directory
from server.engine.transcripts.models import TranscriptStep

if TYPE_CHECKING:
    from server.core.objectstore import ObjectStore

COMPACT_FIELD_LIMIT = 800
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._\-+/=]+"),
    re.compile(r"(?i)(sk-[A-Za-z0-9_-]{12,})"),
    re.compile(r"(?i)(cookie\s*[:=]\s*)[^\r\n;]+"),
)
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_name(value: str) -> str:
    """将 trace_id / span_id 转为安全文件名片段，杜绝路径穿越。"""
    cleaned = _SAFE_SEGMENT.sub("_", value).strip("._")
    if not cleaned:
        raise ValueError(f"无法生成安全文件名：{value!r}")
    return cleaned[:128]


def scrub_text(text: str) -> str:
    """对明文里的 Bearer Token / API Key / Cookie 做二次兜底脱敏。"""
    out = _SECRET_PATTERNS[0].sub(r"\1[REDACTED]", text)
    out = _SECRET_PATTERNS[1].sub("[REDACTED_KEY]", out)
    return _SECRET_PATTERNS[2].sub(r"\1[REDACTED]", out)


def scrub_value(value: Any) -> Any:
    """递归脱敏嵌套字典与列表中的敏感字符串。"""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {str(k): scrub_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrub_value(v) for v in value]
    return value


def compact_payload(payload: dict[str, Any], *, limit: int = COMPACT_FIELD_LIMIT) -> tuple[dict[str, Any], tuple[str, ...]]:
    """生成紧凑版 payload：截断超长字符串字段并返回被截断的字段名列表。"""
    compact: dict[str, Any] = {}
    truncated: list[str] = []
    for key, val in payload.items():
        if isinstance(val, str) and len(val) > limit:
            compact[key] = val[:limit] + f"... [truncated {len(val) - limit} chars]"
            truncated.append(key)
        else:
            compact[key] = val
    return compact, tuple(truncated)


def transcript_relpath(tenant_id: str, trace_id: str, span_id: str, *, at: datetime) -> str:
    """返回相对于 ``<data_dir>/transcripts`` 的路径前缀（不含后缀）。"""
    day = at.astimezone(UTC).strftime("%Y-%m-%d")
    return f"{tenant_directory(tenant_id)}/{day}/{_safe_name(trace_id)}/{_safe_name(span_id)}"


def archive_object_keys(tenant_id: str, trace_id: str, span_id: str) -> tuple[str, str]:
    """生成不可变对象存储归档键 ``(compact_key, full_key)``。"""
    t = _safe_name(trace_id)
    s = _safe_name(span_id)
    return (
        validate_key(f"tenants/{tenant_id}/transcripts/{t}/{s}.compact.jsonl.gz"),
        validate_key(f"tenants/{tenant_id}/transcripts/{t}/{s}.full.jsonl.gz"),
    )


def _write_lines_sync(compact_path: Path, full_path: Path, compact_line: str, full_line: str) -> int:
    """同步安全追加两行并确保 0700/0600 权限；返回本次写入的 full 字节数。"""
    compact_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(compact_path.parent, 0o700)
    full_bytes = full_line.encode("utf-8")
    for path, raw in ((compact_path, compact_line.encode("utf-8")), (full_path, full_bytes)):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
    return len(full_bytes)


class LocalTranscriptWriter:
    """单 span 的双层脱敏本地 JSONL 写入器。"""

    def __init__(
        self,
        data_dir: Path,
        *,
        tenant_id: str,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None = None,
        at: datetime | None = None,
    ) -> None:
        """初始化本地落盘路径（延迟到首次写入时建目录）。"""
        self.tenant_id = tenant_id
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.created_at = at or datetime.now(UTC)
        self.relpath = transcript_relpath(tenant_id, trace_id, span_id, at=self.created_at)
        base = data_dir / "transcripts" / self.relpath
        self.compact_path = Path(f"{base}.compact.jsonl")
        self.full_path = Path(f"{base}.full.jsonl")
        self.line_count = 0
        self.byte_size = 0

    async def append(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        ts: float,
        status: str = "ok",
        duration_ms: int | None = None,
        usage: dict[str, Any] | None = None,
    ) -> TranscriptStep:
        """脱敏并同步追加一条记录到 compact 与 full 文件，保证行号 1-to-1 对齐。"""
        sanitized = scrub_value(sanitize(payload))
        compact_body, truncated = compact_payload(sanitized)
        idx = self.line_count
        full_step = TranscriptStep(
            step_index=idx, ts=ts, trace_id=self.trace_id, span_id=self.span_id,
            parent_span_id=self.parent_span_id, kind=kind, status=status,
            duration_ms=duration_ms, usage=usage, payload=sanitized, truncated_fields=(),
        )
        compact_step = full_step.model_copy(
            update={"payload": compact_body, "truncated_fields": truncated}
        )
        compact_line = compact_step.model_dump_json() + "\n"
        full_line = full_step.model_dump_json() + "\n"
        written = await asyncio.to_thread(
            _write_lines_sync, self.compact_path, self.full_path, compact_line, full_line
        )
        self.line_count += 1
        self.byte_size += written
        return compact_step

    async def archive(self, objects: ObjectStore) -> tuple[str, str]:
        """将已落盘的双层 JSONL 压缩上传至共享对象存储，供跨节点冷回读。"""
        compact_key, full_key = archive_object_keys(self.tenant_id, self.trace_id, self.span_id)
        compact_gz = await asyncio.to_thread(lambda: gzip.compress(self.compact_path.read_bytes()))
        full_gz = await asyncio.to_thread(lambda: gzip.compress(self.full_path.read_bytes()))
        await objects.put(compact_key, compact_gz, content_type="application/gzip")
        await objects.put(full_key, full_gz, content_type="application/gzip")
        return compact_key, full_key


def prune_local_transcripts(
    data_dir: Path, *, max_age_seconds: float, max_total_bytes: int, now: float,
) -> int:
    """按 TTL 与容量上限淘汰最旧的本地热盘 Transcript 文件；返回清理文件数。"""
    root = data_dir / "transcripts"
    if not root.exists():
        return 0
    files: list[tuple[float, int, Path]] = []
    for path in root.rglob("*.jsonl"):
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        files.append((st.st_mtime, st.st_size, path))
    files.sort(key=lambda item: item[0])
    removed = 0
    remaining: list[tuple[float, int, Path]] = []
    for mtime, size, path in files:
        if now - mtime > max_age_seconds:
            path.unlink(missing_ok=True)
            removed += 1
        else:
            remaining.append((mtime, size, path))
    total = sum(size for _, size, _ in remaining)
    for _, size, path in remaining:
        if total <= max_total_bytes:
            break
        path.unlink(missing_ok=True)
        total -= size
        removed += 1
    return removed
