"""长期主题与重点对象的静默追踪器（SOUL M4）。

- 允许为重点公司、行业或人物登记低频持续关注项；
- 每次扫描与上次已汇报基线摘要做 SHA-256 比对：**无实质性新变化时保持绝对静默**；
- 仅当出现新动态且经 ``AttentionBudgetGate(priority="topic_watch")`` 放行时，才轻声汇报并刷新基线。
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from server.core.ids import new_id
from server.core.plugins.collections import PluginCollectionStore

if TYPE_CHECKING:
    from pathlib import Path

    from server.core.tenancy import Principal
    from server.engine.butler.proactive_briefing import AttentionBudgetGate, HabitCalibration


class TopicWatchItem(BaseModel):
    """一条长期静默追踪主题记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    watch_id: str = Field(min_length=1, max_length=80)
    topic: str = Field(min_length=1, max_length=200)
    last_signal_digest: str | None = None
    last_notified_at: float | None = None


class WatchEvaluationResult(BaseModel):
    """单次静默追踪评估结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    notify: bool
    reason: str
    updated_watch: TopicWatchItem
    message: str | None = None


def _normalize_digest(text: str) -> str:
    """对动态摘要做空白归一化后计算 SHA-256，防止仅格式抖动误触发汇报。"""
    normalized = " ".join(text.strip().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def evaluate_watch_signal(
    watch: TopicWatchItem,
    candidate_signal: str | None,
    *,
    user_key: str,
    now: float,
    gate: AttentionBudgetGate,
    timezone: str = "Asia/Shanghai",
    daily_bg_tokens_used: int = 0,
    habit: HabitCalibration | None = None,
) -> WatchEvaluationResult:
    """比对新动态与历史基线：无实质新变化保持静默；有新信号时经注意力预算门控放行汇报。"""
    cleaned = (candidate_signal or "").strip()
    if not cleaned:
        return WatchEvaluationResult(notify=False, reason="no_new_signal", updated_watch=watch)
    digest = _normalize_digest(cleaned)
    if digest == watch.last_signal_digest:
        return WatchEvaluationResult(notify=False, reason="unchanged_signal", updated_watch=watch)

    decision = gate.evaluate(
        user_key,
        priority="topic_watch",
        topic_key=f"watch:{watch.watch_id}",
        now=now,
        timezone=timezone,
        daily_bg_tokens_used=daily_bg_tokens_used,
        habit=habit,
    )
    if not decision.allowed:
        return WatchEvaluationResult(notify=False, reason=decision.reason, updated_watch=watch)

    gate.record_sent(user_key, topic_key=f"watch:{watch.watch_id}", now=now, timezone=timezone)
    updated = watch.model_copy(update={"last_signal_digest": digest, "last_notified_at": now})
    return WatchEvaluationResult(
        notify=True,
        reason="new_signal",
        updated_watch=updated,
        message=f"【持续关注动态 · {watch.topic}】\n{cleaned}",
    )


class TopicWatchStore:
    """基于微内核统一 ``PluginCollectionStore`` 的长期静默追踪存储（PG 共享 / 本地磁盘原子落盘双支持）。"""

    _PLUGIN_ID = "butler:topic-watch"
    _COLLECTION = "watches"

    def __init__(
        self, data_dir: Path, *, collections: PluginCollectionStore | None = None,
    ) -> None:
        """绑定数据目录或平台共享的插件集合存储。"""
        self._collections = collections or PluginCollectionStore(data_dir)

    async def list(self, who: Principal) -> list[TopicWatchItem]:
        """列出本人全部长期静默追踪主题。"""
        rows = await self._collections.list(who, self._PLUGIN_ID, self._COLLECTION, limit=200)
        return [TopicWatchItem.model_validate(r.data) for r in rows]

    async def create(self, who: Principal, topic: str) -> TopicWatchItem:
        """登记一条新的长期静默追踪主题。"""
        created = TopicWatchItem(watch_id=new_id("tw"), topic=topic.strip())
        await self._collections.put(
            who,
            self._PLUGIN_ID,
            self._COLLECTION,
            created.model_dump(mode="json"),
            record_id=created.watch_id,
        )
        return created

    async def delete(self, who: Principal, watch_id: str) -> bool:
        """移除一条追踪主题；不存在抛出 NotFoundError。"""
        return await self._collections.delete(who, self._PLUGIN_ID, self._COLLECTION, watch_id)

    async def save(self, who: Principal, updated: TopicWatchItem) -> TopicWatchItem:
        """持久化更新一条追踪主题的最新基线摘要。"""
        existing = await self._collections.get(who, self._PLUGIN_ID, self._COLLECTION, updated.watch_id)
        await self._collections.put(
            who,
            self._PLUGIN_ID,
            self._COLLECTION,
            updated.model_dump(mode="json"),
            record_id=updated.watch_id,
            expected_version=existing.version,
        )
        return updated
