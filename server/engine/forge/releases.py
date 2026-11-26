"""三层不可变发布节点账本（L1 匠人 / L2 构造器 / L3 内核）与 Clean Unmounting（ADR 0025）。

- 只追加不物理删除：每次切换生成一个单调递增的 ``generation`` 节点（R1 → R2 → R3）；
- 回退同样走追加发布路径（R4 → 指向旧不可变节点 R2），审计链永不丢记录；
- 旧节点 ``retire``（平滑排空）或 ``revoke``（紧急熔断）时，触发 Clean Unmounting 钩子
  通知运行时彻底释放旧版本的引擎池缓存与进程资源。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol

from server.core.controlplane.schema import DEFAULT_NAMESPACE
from server.core.errors import ConflictError, NotFoundError
from server.core.ids import new_id
from server.engine.forge.models import BuiltByProvenance, ReleaseLayer, ReleaseNode, ReleaseState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import asyncpg
    from taifeng_stores import SqlNamespace

    UnmountHook = Callable[[ReleaseNode, Literal["retired", "revoked"]], Awaitable[None]]
    LivenessCheck = Callable[[ReleaseNode], Awaitable[bool]]

logger = logging.getLogger(__name__)


class ReleaseNodeStore(Protocol):
    """不可变发布节点存储协议（Memory 与 PostgreSQL 共用）。"""

    async def append_and_replace(
        self,
        node: ReleaseNode,
        *,
        prior_state: Literal["retired", "revoked"] = "retired",
    ) -> tuple[ReleaseNode, ReleaseNode | None]:
        """原子追加新发布节点并将同 target 原 active 节点置为 retired/revoked；返回 ``(新节点, 被取代旧节点)``。"""
        ...

    async def set_state(
        self, tenant_id: str, release_id: str, state: ReleaseState,
    ) -> ReleaseNode:
        """更新指定发布节点状态（如禁用/熔断）。"""
        ...

    async def get(self, tenant_id: str, release_id: str) -> ReleaseNode:
        """按租户与发布标识读取节点；跨租户与不存在一律 404。"""
        ...

    async def active(
        self, tenant_id: str, layer: ReleaseLayer, target_name: str,
    ) -> ReleaseNode | None:
        """查询当前生效发布节点。"""
        ...

    async def history(
        self, tenant_id: str, layer: ReleaseLayer, target_name: str, *, limit: int = 50,
    ) -> tuple[ReleaseNode, ...]:
        """按代次倒序返回完整发布与回退历史。"""
        ...


class MemoryReleaseNodeStore:
    """进程内发布节点账本。"""

    def __init__(self) -> None:
        """初始化内存字典与互斥锁。"""
        self._lock = asyncio.Lock()
        self._nodes: dict[tuple[str, str], ReleaseNode] = {}

    async def append_and_replace(
        self,
        node: ReleaseNode,
        *,
        prior_state: Literal["retired", "revoked"] = "retired",
    ) -> tuple[ReleaseNode, ReleaseNode | None]:
        """锁内计算下一代次、停用旧 active 节点并写入新节点。"""
        async with self._lock:
            same = [
                r for r in self._nodes.values()
                if r.tenant_id == node.tenant_id
                and r.layer == node.layer
                and r.target_name == node.target_name
            ]
            next_gen = max((r.generation for r in same), default=0) + 1
            prior = next((r for r in same if r.state == "active"), None)
            replaced: ReleaseNode | None = None
            if prior is not None:
                replaced = prior.model_copy(update={"state": prior_state})
                self._nodes[(prior.tenant_id, prior.release_id)] = replaced
            created = node.model_copy(update={
                "generation": next_gen,
                "supersedes": prior.release_id if prior is not None else node.supersedes,
            })
            self._nodes[(created.tenant_id, created.release_id)] = created
            return created, replaced

    async def set_state(
        self, tenant_id: str, release_id: str, state: ReleaseState,
    ) -> ReleaseNode:
        """更新节点状态。"""
        async with self._lock:
            rec = self._nodes.get((tenant_id, release_id))
            if rec is None:
                raise NotFoundError("发布节点不存在")
            updated = rec.model_copy(update={"state": state})
            self._nodes[(tenant_id, release_id)] = updated
            return updated

    async def get(self, tenant_id: str, release_id: str) -> ReleaseNode:
        """读取单个发布节点。"""
        rec = self._nodes.get((tenant_id, release_id))
        if rec is None:
            raise NotFoundError("发布节点不存在")
        return rec

    async def active(
        self, tenant_id: str, layer: ReleaseLayer, target_name: str,
    ) -> ReleaseNode | None:
        """返回当前 active 节点。"""
        for rec in self._nodes.values():
            if (
                rec.tenant_id == tenant_id
                and rec.layer == layer
                and rec.target_name == target_name
                and rec.state == "active"
            ):
                return rec
        return None

    async def history(
        self, tenant_id: str, layer: ReleaseLayer, target_name: str, *, limit: int = 50,
    ) -> tuple[ReleaseNode, ...]:
        """按代次倒序返回历史。"""
        found = [
            r for r in self._nodes.values()
            if r.tenant_id == tenant_id and r.layer == layer and r.target_name == target_name
        ]
        found.sort(key=lambda r: r.generation, reverse=True)
        return tuple(found[:limit])


class PostgresReleaseNodeStore:
    """PostgreSQL 发布节点账本（迁移 0039）。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
        """表名由命名空间派生。"""
        self._pool = pool
        self._table = namespace.table("release_node")

    async def append_and_replace(
        self,
        node: ReleaseNode,
        *,
        prior_state: Literal["retired", "revoked"] = "retired",
    ) -> tuple[ReleaseNode, ReleaseNode | None]:
        """事务内锁定同 target 节点、递增代次并原子切换 active 指针。"""
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND layer=$2 "
                "AND target_name=$3 ORDER BY generation DESC FOR UPDATE",
                node.tenant_id, node.layer, node.target_name,
            )
            existing = [ReleaseNode.model_validate_json(r["record"]) for r in rows]
            next_gen = (existing[0].generation if existing else 0) + 1
            prior = next((r for r in existing if r.state == "active"), None)
            replaced: ReleaseNode | None = None
            if prior is not None:
                replaced = prior.model_copy(update={"state": prior_state})
                await conn.execute(
                    f"UPDATE {self._table} SET state=$3,record=$4 WHERE tenant_id=$1 AND release_id=$2",
                    replaced.tenant_id, replaced.release_id, replaced.state, replaced.model_dump_json(),
                )
            created = node.model_copy(update={
                "generation": next_gen,
                "supersedes": prior.release_id if prior is not None else node.supersedes,
            })
            await conn.execute(
                f"INSERT INTO {self._table} (tenant_id,release_id,layer,target_name,generation,"
                "state,created_at,record) VALUES($1,$2,$3,$4,$5,$6,$7,$8)",
                created.tenant_id, created.release_id, created.layer, created.target_name,
                created.generation, created.state, created.created_at, created.model_dump_json(),
            )
            return created, replaced

    async def set_state(
        self, tenant_id: str, release_id: str, state: ReleaseState,
    ) -> ReleaseNode:
        """事务内更新节点状态。"""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND release_id=$2 FOR UPDATE",
                tenant_id, release_id,
            )
            if row is None:
                raise NotFoundError("发布节点不存在")
            rec = ReleaseNode.model_validate_json(row["record"])
            updated = rec.model_copy(update={"state": state})
            await conn.execute(
                f"UPDATE {self._table} SET state=$3,record=$4 WHERE tenant_id=$1 AND release_id=$2",
                tenant_id, release_id, state, updated.model_dump_json(),
            )
            return updated

    async def get(self, tenant_id: str, release_id: str) -> ReleaseNode:
        """按租户与发布标识读节点。"""
        row = await self._pool.fetchrow(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND release_id=$2",
            tenant_id, release_id,
        )
        if row is None:
            raise NotFoundError("发布节点不存在")
        return ReleaseNode.model_validate_json(row["record"])

    async def active(
        self, tenant_id: str, layer: ReleaseLayer, target_name: str,
    ) -> ReleaseNode | None:
        """查询当前 active 节点。"""
        row = await self._pool.fetchrow(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND layer=$2 "
            "AND target_name=$3 AND state='active' ORDER BY generation DESC LIMIT 1",
            tenant_id, layer, target_name,
        )
        return None if row is None else ReleaseNode.model_validate_json(row["record"])

    async def history(
        self, tenant_id: str, layer: ReleaseLayer, target_name: str, *, limit: int = 50,
    ) -> tuple[ReleaseNode, ...]:
        """按代次倒序返回历史。"""
        rows = await self._pool.fetch(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND layer=$2 "
            "AND target_name=$3 ORDER BY generation DESC LIMIT $4",
            tenant_id, layer, target_name, limit,
        )
        return tuple(ReleaseNode.model_validate_json(r["record"]) for r in rows)


class ReleaseLedgerService:
    """三层发布节点管理服务：原子晋升、活性复核回退、禁用熔断与 Clean Unmounting。"""

    def __init__(
        self, store: ReleaseNodeStore, *, now: Callable[[], float] = time.time,
    ) -> None:
        """依赖由平台注入。"""
        self._store = store
        self._now = now
        self.unmount_hooks: list[UnmountHook] = []

    async def _notify_unmount(
        self, node: ReleaseNode, state: Literal["retired", "revoked"],
    ) -> None:
        """触发 Clean Unmounting 钩子释放旧版本缓存/资源，单个失败记日志。"""
        for hook in self.unmount_hooks:
            try:
                await hook(node, state)
            except Exception:
                logger.exception("发布节点 %s Clean Unmounting 回调失败", node.release_id)

    async def promote(
        self,
        tenant_id: str,
        *,
        layer: ReleaseLayer,
        target_name: str,
        version: str,
        artifact_digest: str,
        actor: str,
        built_by: BuiltByProvenance | None = None,
        evidence_ref: str | None = None,
        prior_state: Literal["retired", "revoked"] = "retired",
    ) -> ReleaseNode:
        """发布一个新不可变节点并原子接管生效指针。"""
        node = ReleaseNode(
            release_id=new_id("rel"),
            tenant_id=tenant_id,
            layer=layer,
            target_name=target_name,
            generation=1,
            version=version,
            artifact_digest=artifact_digest,
            state="active",
            built_by=built_by,
            evidence_ref=evidence_ref,
            actor=actor,
            created_at=datetime.fromtimestamp(self._now(), UTC),
        )
        created, replaced = await self._store.append_and_replace(node, prior_state=prior_state)
        if replaced is not None:
            await self._notify_unmount(replaced, prior_state)
        return created

    async def rollback(
        self,
        tenant_id: str,
        *,
        layer: ReleaseLayer,
        target_name: str,
        to_release_id: str,
        actor: str,
        prior_state: Literal["retired", "revoked"] = "retired",
        liveness_check: LivenessCheck | None = None,
    ) -> ReleaseNode:
        """回退到历史不可变发布节点：追加新代次节点指向目标制品，绝不删历史。"""
        target = await self._store.get(tenant_id, to_release_id)
        if target.layer != layer or target.target_name != target_name:
            raise ConflictError("回退目标节点与当前层或目标名称不匹配")
        if liveness_check is not None and not await liveness_check(target):
            raise ConflictError("回退目标发布节点依赖已失效，无法安全回退")
        current = await self._store.active(tenant_id, layer, target_name)
        node = ReleaseNode(
            release_id=new_id("rel"),
            tenant_id=tenant_id,
            layer=layer,
            target_name=target_name,
            generation=1,
            version=target.version,
            artifact_digest=target.artifact_digest,
            state="active",
            rollback_of=current.release_id if current is not None else None,
            built_by=target.built_by,
            evidence_ref=target.evidence_ref,
            actor=actor,
            created_at=datetime.fromtimestamp(self._now(), UTC),
        )
        created, replaced = await self._store.append_and_replace(node, prior_state=prior_state)
        if replaced is not None:
            await self._notify_unmount(replaced, prior_state)
        return created

    async def disable(
        self,
        tenant_id: str,
        *,
        layer: ReleaseLayer,
        target_name: str,
        revoke: bool = True,
    ) -> ReleaseNode:
        """禁用或紧急熔断当前生效节点并触发 Clean Unmounting。"""
        current = await self._store.active(tenant_id, layer, target_name)
        if current is None:
            raise NotFoundError("当前没有生效中的发布节点")
        new_state: Literal["retired", "revoked"] = "revoked" if revoke else "retired"
        updated = await self._store.set_state(tenant_id, current.release_id, new_state)
        await self._notify_unmount(updated, new_state)
        return updated

    async def active(
        self, tenant_id: str, layer: ReleaseLayer, target_name: str,
    ) -> ReleaseNode | None:
        """查询当前生效发布节点。"""
        return await self._store.active(tenant_id, layer, target_name)

    async def history(
        self, tenant_id: str, layer: ReleaseLayer, target_name: str, *, limit: int = 50,
    ) -> tuple[ReleaseNode, ...]:
        """查询完整追加式发布历史。"""
        return await self._store.history(tenant_id, layer, target_name, limit=limit)
