"""内存双账本的准备提交；PG在已有SQL事务内直接更新。"""
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from xuanpu_server.controlplane.models import UsageRecord
from xuanpu_server.identity.models import UserUsageRecord
from xuanpu_server.work_tasks.budget_store import usage_deltas

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.workspace.models import WorkspaceState


def bind_memory_usage(store: Any, control: Any, identity: Any) -> None:
    """仅明确声明内存接口的后端安装同步原子提交器。"""
    if not hasattr(store, 'bind_task_usage'):
        return

    def prepare(tenant: str, user: str, old: WorkspaceState, state: WorkspaceState) -> Callable[[], None]:
        """所有数值先验证，再在无await区间提交两本用量。"""
        tenants: dict[str, UsageRecord] = {}
        users: dict[str, UserUsageRecord] = {}
        for day, (inputs, outputs, total, turns) in usage_deltas(old, state).items():
            current = control._usage.get((tenant, day), UsageRecord(tenant, day))
            employee = identity._usage.get((tenant, day, user), UserUsageRecord(tenant, day, user))
            for target, record in ((tenants, current), (users, employee)):
                target[day] = replace(record, input_tokens=record.input_tokens + inputs,
                    output_tokens=record.output_tokens + outputs, total_tokens=record.total_tokens + total,
                    turns=record.turns + turns)

        def commit() -> None:
            """员工事务与审计成功后一次提交，不能出现半本账。"""
            for day in tenants:
                control._usage[(tenant, day)] = tenants[day]
                identity._usage[(tenant, day, user)] = users[day]

        return commit

    store.bind_task_usage(prepare)
