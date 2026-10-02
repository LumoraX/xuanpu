"""审计记录的写入。

审计来自三处（ADR 0004 决策 4）：内核事件总线、效应面闸门、控制面操作。三处都经
``AuditLog`` 落到控制面存储；记录只追加。

写审计不允许悄悄失败：闸门与控制面的记录同步写入，写不进去则操作本身失败；引擎事件
在后台成批写入，写不进去时记错误日志并保留在缓冲里等下一次重试。例外是人工拒绝的补记
（``runtime.gate_audit``）：它在内核确认核销之后才写，那时送回已经交给内核，写不进去记错误
日志并计入补记失败的指标，不让送回失败。

给了指标（``PlatformMetrics``）时，这里也是两项指标的打点处：每次写失败计一次
``audit_write_failures``；每条过闸记录（闸门的放行与拒绝、人工拒绝的补记都经 ``gate``）按
传输与决定计一次 ``connector_calls``——不论记录写没写成，调用（或拒绝）都已经发生了。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from xuanpu_server.controlplane.models import AuditRecord

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.metrics import PlatformMetrics
    from xuanpu_server.tenancy import Principal

logger = logging.getLogger(__name__)

# 流式增量事件：量大，而且其内容在随后的完成事件与消息记录里都有
STREAMING_KINDS: frozenset[str] = frozenset({"assistant_text", "assistant_reasoning"})
# 含完整提示词与对话正文的事件：只在租户显式开启留痕时写入
SENSITIVE_KINDS: frozenset[str] = frozenset({"llm_request_recorded"})

_REDACTED = "[已脱敏]"
# 键名里出现这些整词就当作凭据（按 _ - . 与大小写切词；"input_tokens" 切出的是 "tokens"，不算）
_SENSITIVE_WORDS = frozenset(
    {
        "secret",
        "secrets",
        "password",
        "passwd",
        "token",
        "apikey",
        "authorization",
        "credential",
        "credentials",
    }
)
_MAX_STRING = 8000
_MAX_DEPTH = 8
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def _looks_sensitive(key: str) -> bool:
    """键名看起来像是装凭据的。"""
    words = [word for word in _WORD_SPLIT.split(key.lower()) if word]
    if any(word in _SENSITIVE_WORDS for word in words):
        return True
    # api_key / apiKey 切出来是 api + key
    return any(a == "api" and b == "key" for a, b in zip(words, words[1:], strict=False))


def sanitize(value: Any, *, depth: int = 0) -> Any:
    """把事件内容整理成可以落库的 JSON：脱敏、截断、去掉不可序列化的对象。

    按键名脱敏是兜底手段，不是密钥不外泄的依据——依据是明文只在效应面出口解析
    （``xuanpu_server.secrets``），根本不经过事件。
    """
    if depth > _MAX_DEPTH:
        return "[嵌套过深，已截断]"
    if isinstance(value, dict):
        return {
            # 只脱敏字符串：数字（如 token 用量）不是凭据
            str(key): _REDACTED
            if isinstance(item, str) and _looks_sensitive(str(key))
            else sanitize(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [sanitize(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        if len(value) > _MAX_STRING:
            return value[:_MAX_STRING] + f"…[共 {len(value)} 字符，已截断]"
        return value
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)


def digest_arguments(arguments: Any) -> str:
    """调用参数的摘要：审计里默认只记摘要，既能对账又不留存参数里的个人信息。"""
    canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditLog:
    """审计写入口。"""

    def __init__(
        self,
        store: ControlPlaneStore,
        *,
        now: Callable[[], float] = time.time,
        metrics: PlatformMetrics | None = None,
    ) -> None:
        """
        Args:
            store: 控制面存储。
            now: 取当前时间的函数，测试时可替换。
            metrics: 平台指标；None 不打点。
        """
        self._store = store
        self._now = now
        self._metrics = metrics

    async def _append(self, records: list[AuditRecord]) -> int:
        """写入记录。写失败计一次指标，异常原样上抛。"""
        try:
            return await self._store.append_audit(records)
        except Exception:
            if self._metrics is not None:
                self._metrics.audit_write_failed()
            raise

    async def control(
        self,
        principal: Principal,
        tenant_id: str,
        kind: str,
        data: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> None:
        """记一条控制面操作。``user_id`` 取调用者代表的员工（浏览器登录或代员工调用）。"""
        await self._append(
            [
                AuditRecord(
                    tenant_id=tenant_id,
                    source="control",
                    kind=kind,
                    at=self._now(),
                    actor=principal.actor,
                    session_id=session_id,
                    data=sanitize(data or {}),
                    user_id=principal.user_id,
                )
            ]
        )

    async def gate(
        self,
        tenant_id: str,
        kind: str,
        data: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> None:
        """记一次过闸。``connector_call`` 先按 ``data`` 里的传输与决定计一次调用，再写。"""
        if kind == "connector_call" and self._metrics is not None:
            self._metrics.connector_call(str(data["transport"]), str(data["decision"]))
        await self._append(
            [
                AuditRecord(
                    tenant_id=tenant_id,
                    source="gate",
                    kind=kind,
                    at=self._now(),
                    actor="engine",
                    session_id=session_id,
                    data=sanitize(data),
                )
            ]
        )

    async def engine(self, records: list[AuditRecord]) -> int:
        """成批写入引擎事件。"""
        return await self._append(records)


__all__ = [
    "SENSITIVE_KINDS",
    "STREAMING_KINDS",
    "AuditLog",
    "digest_arguments",
    "sanitize",
]
