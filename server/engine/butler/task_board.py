"""管家跨轮持久任务板（ADR 0024）：解决每轮新建引擎导致的跨轮失忆并提供实时焦点。

- 按 ``(tenant_id, user_id)`` 强隔离，每条活跃 ``trace_id`` 对应一块任务板。
- 步骤支持 ``blocked_by`` 有向无环图（DAG）依赖，提交时做重名、未知引用与拓扑环检测。
- 活跃任务板以 JSON 数据字段注入 ``UserMessage`` payload（``working_context.task_boards``），
  绝不混入静态 ``system_prompt``，保住 Prompt Cache 并防止数据越权为指令。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime  # noqa: TC003 —— pydantic 运行时解析
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from server.core.controlplane.schema import DEFAULT_NAMESPACE
from server.core.errors import ConflictError, NotFoundError

if TYPE_CHECKING:
    import asyncpg
    from taifeng_stores import SqlNamespace

    from server.engine.tracing.models import TraceOwner

BoardStepStatus = Literal["pending", "in_progress", "done", "skipped", "failed"]
BoardStatus = Literal["active", "completed", "failed", "cancelled"]
BOARD_TERMINAL: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
_STEP_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,48}$")
_REF_ID = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")


class TaskBoardStep(BaseModel):
    """任务板中的单个步骤，支持显式前序依赖 ``blocked_by``。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(min_length=1, max_length=48)
    title: str = Field(min_length=1, max_length=120)
    status: BoardStepStatus = "pending"
    blocked_by: tuple[str, ...] = Field(default=(), max_length=12)
    ref: str | None = Field(default=None, max_length=160)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        """步骤标识限定安全字符集。"""
        if not _STEP_ID.fullmatch(value):
            raise ValueError("步骤标识只能是 1–48 位字母、数字与 _.:-")
        return value

    @field_validator("blocked_by")
    @classmethod
    def _valid_deps(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """去重并校验依赖标识格式。"""
        if len(set(value)) != len(value):
            raise ValueError("blocked_by 不能包含重复依赖")
        for dep in value:
            if not _STEP_ID.fullmatch(dep):
                raise ValueError(f"无效依赖步骤标识：{dep}")
        return value

    @field_validator("ref")
    @classmethod
    def _valid_ref(cls, value: str | None) -> str | None:
        """关联派工/编组引用走标识校验，禁止写入正文。"""
        if value is not None and not _REF_ID.fullmatch(value):
            raise ValueError("引用标识格式不合法")
        return value


def _validate_dag(steps: tuple[TaskBoardStep, ...]) -> None:
    """校验步骤标识唯一、依赖目标存在且无拓扑环。"""
    by_id: dict[str, TaskBoardStep] = {}
    for step in steps:
        if step.id in by_id:
            raise ValueError(f"重复的步骤标识：{step.id}")
        by_id[step.id] = step
    for step in steps:
        for dep in step.blocked_by:
            if dep == step.id:
                raise ValueError(f"步骤 {step.id} 不能依赖自身")
            if dep not in by_id:
                raise ValueError(f"步骤 {step.id} 引用了不存在的依赖：{dep}")
    # Kahn 拓扑排序检测环
    indegree = {s.id: len(s.blocked_by) for s in steps}
    dependents: dict[str, list[str]] = {s.id: [] for s in steps}
    for s in steps:
        for dep in s.blocked_by:
            dependents[dep].append(s.id)
    queue = [sid for sid, deg in indegree.items() if deg == 0]
    visited = 0
    while queue:
        cur = queue.pop()
        visited += 1
        for nxt in dependents[cur]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if visited != len(steps):
        raise ValueError("任务板步骤依赖存在循环")


class TaskBoardRecord(BaseModel):
    """一条跨轮持久任务板记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    board_id: str = Field(min_length=1, max_length=80)
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=160)
    focus: str | None = Field(default=None, max_length=200)
    status: BoardStatus = "active"
    steps: tuple[TaskBoardStep, ...] = Field(default=(), max_length=24)
    version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _check_steps(self) -> TaskBoardRecord:
        """校验步骤 DAG 合法性。"""
        _validate_dag(self.steps)
        return self

    @property
    def active(self) -> bool:
        """是否仍在进行中。"""
        return self.status not in BOARD_TERMINAL


def ready_steps(board: TaskBoardRecord) -> tuple[TaskBoardStep, ...]:
    """返回所有前序依赖已完成、此刻可以启动的 ``pending`` 步骤。"""
    done_ids = {s.id for s in board.steps if s.status in ("done", "skipped")}
    return tuple(
        s for s in board.steps
        if s.status == "pending" and all(dep in done_ids for dep in s.blocked_by)
    )


def board_view(board: TaskBoardRecord) -> dict[str, Any]:
    """接口与 Working Context 共用的精简视图（不暴露租户与内部用户标识）。"""
    return {
        "board_id": board.board_id,
        "trace_id": board.trace_id,
        "title": board.title,
        "focus": board.focus,
        "status": board.status,
        "steps": [s.model_dump(mode="json") for s in board.steps],
        "ready_step_ids": [s.id for s in ready_steps(board)],
        "version": board.version,
        "updated_at": board.updated_at.isoformat(),
    }


class TaskBoardStore(Protocol):
    """跨轮持久任务板存储协议（内存与 PostgreSQL 共用）。"""

    async def upsert(
        self, owner: TraceOwner, record: TaskBoardRecord, *, expected_version: int | None = None,
    ) -> TaskBoardRecord:
        """按 ``(owner, trace_id)`` 创建或 CAS 更新任务板。"""
        ...

    async def get(self, owner: TraceOwner, board_id: str) -> TaskBoardRecord:
        """读取本人指定任务板；他人与不存在一律 404。"""
        ...

    async def find_by_trace(self, owner: TraceOwner, trace_id: str) -> TaskBoardRecord | None:
        """按 trace 读取本人任务板；不存在返回 None。"""
        ...

    async def list_active(self, owner: TraceOwner, *, limit: int = 5) -> tuple[TaskBoardRecord, ...]:
        """读取本人活跃任务板，按最近更新时间倒序。"""
        ...

    async def list_recent(self, owner: TraceOwner, *, limit: int = 20) -> tuple[TaskBoardRecord, ...]:
        """读取本人近期全部任务板（含已完结），按最近更新时间倒序。"""
        ...


def _check_owner(owner: TraceOwner, record: TaskBoardRecord) -> None:
    """校验归属一致。"""
    if (record.tenant_id, record.user_id) != (owner.tenant_id, owner.user_id):
        raise ConflictError("任务板归属与调用方不符")


class MemoryTaskBoardStore:
    """进程内任务板存储（用于测试与单机模式）。"""

    def __init__(self) -> None:
        """初始化内存表与互斥锁。"""
        self._lock = asyncio.Lock()
        self._boards: dict[tuple[str, str], TaskBoardRecord] = {}
        self._by_trace: dict[tuple[str, str, str], str] = {}

    async def upsert(
        self, owner: TraceOwner, record: TaskBoardRecord, *, expected_version: int | None = None,
    ) -> TaskBoardRecord:
        """同 trace 幂等创建或版本 CAS 更新。"""
        _check_owner(owner, record)
        async with self._lock:
            tkey = (owner.tenant_id, owner.user_id, record.trace_id)
            existing_id = self._by_trace.get(tkey)
            if existing_id is None:
                if expected_version is not None:
                    raise ConflictError("任务板版本不匹配")
                self._boards[(owner.tenant_id, record.board_id)] = record
                self._by_trace[tkey] = record.board_id
                return record
            current = self._boards[(owner.tenant_id, existing_id)]
            if expected_version is not None and current.version != expected_version:
                raise ConflictError("任务板版本已变化，请刷新后重试")
            updated = record.model_copy(update={
                "board_id": current.board_id,
                "version": current.version + 1,
                "created_at": current.created_at,
            })
            self._boards[(owner.tenant_id, current.board_id)] = updated
            return updated

    async def get(self, owner: TraceOwner, board_id: str) -> TaskBoardRecord:
        """读取本人任务板。"""
        record = self._boards.get((owner.tenant_id, board_id))
        if record is None or record.user_id != owner.user_id:
            raise NotFoundError("任务板不存在")
        return record

    async def find_by_trace(self, owner: TraceOwner, trace_id: str) -> TaskBoardRecord | None:
        """按 trace 查找本人任务板。"""
        board_id = self._by_trace.get((owner.tenant_id, owner.user_id, trace_id))
        if board_id is None:
            return None
        return self._boards.get((owner.tenant_id, board_id))

    async def list_active(self, owner: TraceOwner, *, limit: int = 5) -> tuple[TaskBoardRecord, ...]:
        """按更新时间倒序返回活跃任务板。"""
        found = [
            b for b in self._boards.values()
            if b.tenant_id == owner.tenant_id and b.user_id == owner.user_id and b.active
        ]
        found.sort(key=lambda b: (b.updated_at, b.board_id), reverse=True)
        return tuple(found[:limit])

    async def list_recent(self, owner: TraceOwner, *, limit: int = 20) -> tuple[TaskBoardRecord, ...]:
        """按更新时间倒序返回近期任务板。"""
        found = [
            b for b in self._boards.values()
            if b.tenant_id == owner.tenant_id and b.user_id == owner.user_id
        ]
        found.sort(key=lambda b: (b.updated_at, b.board_id), reverse=True)
        return tuple(found[:limit])


class PostgresTaskBoardStore:
    """PostgreSQL 任务板存储（迁移 0036）。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
        """表名由命名空间派生。"""
        self._pool = pool
        self._table = namespace.table("butler_task_board")

    async def upsert(
        self, owner: TraceOwner, record: TaskBoardRecord, *, expected_version: int | None = None,
    ) -> TaskBoardRecord:
        """事务内锁定同 trace 任务板，按需核对 CAS 版本后落盘。"""
        _check_owner(owner, record)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND user_id=$2 "
                "AND trace_id=$3 FOR UPDATE",
                owner.tenant_id, owner.user_id, record.trace_id,
            )
            if row is None:
                if expected_version is not None:
                    raise ConflictError("任务板版本不匹配")
                await conn.execute(
                    f"INSERT INTO {self._table} (tenant_id,board_id,user_id,trace_id,active,"
                    "updated_at,record) VALUES($1,$2,$3,$4,$5,$6,$7)",
                    record.tenant_id, record.board_id, record.user_id, record.trace_id,
                    record.active, record.updated_at, record.model_dump_json(),
                )
                return record
            current = TaskBoardRecord.model_validate_json(row["record"])
            if expected_version is not None and current.version != expected_version:
                raise ConflictError("任务板版本已变化，请刷新后重试")
            updated = record.model_copy(update={
                "board_id": current.board_id,
                "version": current.version + 1,
                "created_at": current.created_at,
            })
            await conn.execute(
                f"UPDATE {self._table} SET active=$3,updated_at=$4,record=$5 "
                "WHERE tenant_id=$1 AND board_id=$2",
                updated.tenant_id, updated.board_id, updated.active,
                updated.updated_at, updated.model_dump_json(),
            )
            return updated

    async def get(self, owner: TraceOwner, board_id: str) -> TaskBoardRecord:
        """读取本人任务板。"""
        row = await self._pool.fetchrow(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND user_id=$2 AND board_id=$3",
            owner.tenant_id, owner.user_id, board_id,
        )
        if row is None:
            raise NotFoundError("任务板不存在")
        return TaskBoardRecord.model_validate_json(row["record"])

    async def find_by_trace(self, owner: TraceOwner, trace_id: str) -> TaskBoardRecord | None:
        """按 trace 查找本人任务板。"""
        row = await self._pool.fetchrow(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND user_id=$2 AND trace_id=$3",
            owner.tenant_id, owner.user_id, trace_id,
        )
        return None if row is None else TaskBoardRecord.model_validate_json(row["record"])

    async def list_active(self, owner: TraceOwner, *, limit: int = 5) -> tuple[TaskBoardRecord, ...]:
        """按更新时间倒序返回本人活跃任务板。"""
        rows = await self._pool.fetch(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND user_id=$2 AND active "
            "ORDER BY updated_at DESC, board_id DESC LIMIT $3",
            owner.tenant_id, owner.user_id, limit,
        )
        return tuple(TaskBoardRecord.model_validate_json(r["record"]) for r in rows)

    async def list_recent(self, owner: TraceOwner, *, limit: int = 20) -> tuple[TaskBoardRecord, ...]:
        """按更新时间倒序返回本人近期任务板。"""
        rows = await self._pool.fetch(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND user_id=$2 "
            "ORDER BY updated_at DESC, board_id DESC LIMIT $3",
            owner.tenant_id, owner.user_id, limit,
        )
        return tuple(TaskBoardRecord.model_validate_json(r["record"]) for r in rows)
