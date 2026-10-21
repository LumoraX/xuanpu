"""个人工作台 PostgreSQL 事务；所有表与控制面共用命名空间。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from xuanpu_server.controlplane.schema import DEFAULT_NAMESPACE
from xuanpu_server.errors import PermissionDeniedError
from xuanpu_server.work_tasks.activity import ActivityConfig
from xuanpu_server.workspace.models import SettingsData, WorkspaceState
from xuanpu_server.workspace.postgres_proactive import load_proactive, save_proactive
from xuanpu_server.workspace.postgres_reminders import save_reminders
from xuanpu_server.workspace.postgres_task_execution import load_execution, save_execution
from xuanpu_server.workspace.postgres_work_tasks import load_tasks, save_tasks
from xuanpu_server.workspace.rules import ensure_todo_progress

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import asyncpg
    from taifeng_stores import SqlNamespace


class PostgresWorkspaceStore:
    """租户共享锁、员工排他锁后统一校验和提交领域快照。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
        """表名来自已验证的 SqlNamespace，用户数据全为绑定参数。"""
        self._pool = pool
        self._activity_config = ActivityConfig()
        self._t = {
            name: namespace.table(name)
            for name in (
                "tenant",
                "user",
                "audit",
                "workspace_settings",
                "workspace_item",
                "workspace_file_version",
                "workspace_request",
                "workspace_message",
                "proactive_check",
                "proactive_followup",
                "proactive_scan_state",
                "notification",
                "notification_delivery",
                "reminder",
                "work_task",
                "work_task_input",
                "work_task_event",
                "task_definition", "task_definition_version", "task_definition_input", "task_occurrence", "task_occurrence_input", "task_domain_event", "task_event_delivery",
                "task_grant", "task_dispatch", "task_attempt", "task_budget_reservation", "task_effect", "usage", "user_usage", "api_key",
            )
        }

    def configure_activity(self, config: ActivityConfig) -> None:
        """注入服务端工作动态灰度，员工请求不能改变此配置。"""
        self._activity_config = config

    async def trigger_backlog(self) -> dict[str, int | float | None]:
        """数据库聚合积压，只暴露数量和最老时间供内部运维观测。"""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(f"SELECT COUNT(*) AS unsettled,MIN(e.occurred_at) AS oldest_at "
                f"FROM {self._t['task_event_delivery']} d JOIN {self._t['task_domain_event']} e "
                "USING(tenant_id,user_id,event_id) WHERE d.status!='settled'")
        assert row is not None
        return {'unsettled': row['unsettled'], 'oldest_at': row['oldest_at']}

    async def trigger_candidates(self,kind: str, *, after: tuple[str,str,str] | None=None,limit: int=100,now: float) -> list[tuple[str,str,str]]:
        """时间和事件查询均受服务端批次上限。"""
        from xuanpu_server.task_triggers.postgres import candidates

        if kind not in {'time','event'} or not 1<=limit<=100:
            raise ValueError('触发候选参数无效')
        return await candidates(self._pool,self._t,kind,after=after,limit=limit,now=now)

    async def trigger_occurrences(self,tenant: str,user: str,identifier: str, *, after: str | None=None,limit: int=100) -> list[dict[str,Any]]:
        """本人历史分页，调用者先经领域身份检查。"""
        from xuanpu_server.task_triggers.postgres import occurrences

        return await occurrences(self._pool,self._t,tenant,user,identifier,after,limit)

    async def _lock(self, conn: Any, tenant: str, user: str, *, allow_inactive: bool = False, exclusive: bool = False) -> None:
        """统一先租户后员工，与身份停用和提醒事务同序。"""
        active_tenant = await conn.fetchval(
            f"SELECT status FROM {self._t['tenant']} WHERE tenant_id=$1 FOR " + ('UPDATE' if exclusive else 'SHARE'), tenant
        )
        active_user = await conn.fetchval(
            f"SELECT status FROM {self._t['user']} WHERE tenant_id=$1 AND user_id=$2 FOR UPDATE",
            tenant,
            user,
        )
        if (active_tenant is None or active_user is None
            or (not allow_inactive and (active_tenant != "active" or active_user != "active"))):
            raise PermissionDeniedError("员工或租户不可用")

    @asynccontextmanager
    async def transaction(
        self,
        tenant: str,
        user: str,
        guard: Callable[[], Awaitable[None]],
        request_id: str | None = None,
        *, allow_inactive: bool = False,
    ) -> AsyncIterator[WorkspaceState]:
        """同一事务包含 CAS、关系、空间、幂等与审计写入。"""
        async with self._pool.acquire() as conn, conn.transaction():
            if request_id and request_id.startswith('work-task:exec:'):
                await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',
                                   self._t['task_dispatch'])
            await self._lock(conn, tenant, user, allow_inactive=allow_inactive,
                             exclusive=bool(request_id and request_id.startswith(('work-task:budget:', 'work-task:exec:'))))
            state = await self._load(conn, tenant, user, request_id)
            state.transaction_at = float(await conn.fetchval('SELECT EXTRACT(EPOCH FROM clock_timestamp())'))
            original = deepcopy(state)
            from xuanpu_server.transaction_reads import transaction_reads

            with transaction_reads(self._pool,conn):
                yield state
            from xuanpu_server.work_tasks.execution_context import current_execution
            from xuanpu_server.work_tasks.execution_store import assert_lease

            binding = current_execution.get()
            if binding is not None and binding.store.database_clock and not allow_inactive:
                at = float(await conn.fetchval('SELECT EXTRACT(EPOCH FROM clock_timestamp())'))
                assert_lease(state, binding.lease, at)
            from xuanpu_server.task_triggers.outbox import derive

            derive(original,state)
            from xuanpu_server.work_tasks.activity import derive as derive_activity

            derive_activity(original,state,enabled=self._activity_config.allows(tenant))
            await self._save(conn, tenant, user, original, state)
        # 只有真实数据库提交成功才消费事务内队列；审计或提交失败时保留原内容。
        state.pending_notifications = []
        state.pending_reminders = []
        state.pending_reminder_actors = {}
        state.pending_reminder_cancellations = []

    async def _load(
        self, conn: Any, tenant: str, user: str, request_id: str | None
    ) -> WorkspaceState:
        """加载员工有界元数据；不加载全部历史对话和请求。"""
        state = WorkspaceState()
        rows = await conn.fetch(
            f"SELECT item_id,record FROM {self._t['workspace_item']} WHERE tenant_id=$1 AND user_id=$2",
            tenant,
            user,
        )
        state.items = {r["item_id"]: json.loads(r["record"]) for r in rows}
        for item in state.items.values():
            ensure_todo_progress(item)
        rows = await conn.fetch(
            f"SELECT item_id,record FROM {self._t['workspace_file_version']} WHERE tenant_id=$1 AND user_id=$2 ORDER BY item_id,revision",
            tenant,
            user,
        )
        for row in rows:
            state.versions.setdefault(row["item_id"], []).append(json.loads(row["record"]))
        settings = await conn.fetchrow(
            f"SELECT settings,next_message_id FROM {self._t['workspace_settings']} WHERE tenant_id=$1 AND user_id=$2",
            tenant,
            user,
        )
        if settings is not None:
            state.settings, state.next_message_id = (
                json.loads(settings["settings"]),
                settings["next_message_id"],
            )
        state.settings = {**SettingsData().model_dump(), **state.settings}
        await load_proactive(conn, self._t, tenant, user, state)
        if request_id is not None:
            keys = [request_id]
            if request_id.startswith("proposal:"):
                keys.append(request_id.removeprefix("proposal:") + ":create")
            elif request_id.endswith(":create"):
                keys.append("proposal:" + request_id.removesuffix(":create"))
            if request_id.startswith('work-task:reconcile-request:'):
                keys.append('reconcile:'+request_id.removeprefix('work-task:reconcile-request:'))
            if request_id.startswith('work-task:trigger-request:'):
                keys.append('trigger:'+request_id.removeprefix('work-task:trigger-request:'))
            requests = await conn.fetch(
                f"SELECT request_id,fingerprint,result FROM {self._t['workspace_request']} WHERE tenant_id=$1 AND user_id=$2 AND request_id=ANY($3::text[])",
                tenant,
                user,
                keys,
            )
            for old in requests:
                state.requests[old["request_id"]] = {
                    "fingerprint": old["fingerprint"],
                    "result": json.loads(old["result"]),
                }
        from xuanpu_server.task_triggers.postgres import load as load_triggers

        await load_triggers(conn,self._t,tenant,user,state,request_id)
        await load_tasks(conn, self._t, tenant, user, state, request_id)
        await load_execution(conn, self._t, tenant, user, state, request_id)
        return state

    async def _save(
        self, conn: Any, tenant: str, user: str, old: WorkspaceState, state: WorkspaceState
    ) -> None:
        """仅写变更元数据；文件版本、对话及幂等响应只追加。"""
        for item_id, item in state.items.items():
            if old.items.get(item_id) != item:
                await conn.execute(
                    f"INSERT INTO {self._t['workspace_item']} (tenant_id,user_id,item_id,kind,record) VALUES ($1,$2,$3,$4,$5::jsonb) ON CONFLICT (tenant_id,user_id,item_id) DO UPDATE SET record=EXCLUDED.record",
                    tenant,
                    user,
                    item_id,
                    item["kind"],
                    json.dumps(item),
                )
        for item_id, versions in state.versions.items():
            for version in versions[len(old.versions.get(item_id, [])) :]:
                await conn.execute(
                    f"INSERT INTO {self._t['workspace_file_version']} (tenant_id,user_id,item_id,revision,record) VALUES ($1,$2,$3,$4,$5::jsonb)",
                    tenant,
                    user,
                    item_id,
                    version["revision"],
                    json.dumps(version),
                )
        if old.settings != state.settings or old.next_message_id != state.next_message_id:
            await conn.execute(
                f"INSERT INTO {self._t['workspace_settings']} (tenant_id,user_id,settings,next_message_id) VALUES ($1,$2,$3::jsonb,$4) ON CONFLICT (tenant_id,user_id) DO UPDATE SET settings=EXCLUDED.settings,next_message_id=EXCLUDED.next_message_id",
                tenant,
                user,
                json.dumps(state.settings),
                state.next_message_id,
            )
        for request_id, request in state.requests.items():
            if old.requests.get(request_id) != request:
                await conn.execute(
                    f"INSERT INTO {self._t['workspace_request']} (tenant_id,user_id,request_id,fingerprint,result) VALUES ($1,$2,$3,$4,$5::jsonb) ON CONFLICT (tenant_id,user_id,request_id) DO UPDATE SET result=EXCLUDED.result",
                    tenant,
                    user,
                    request_id,
                    request["fingerprint"],
                    json.dumps(request["result"]),
                )
        for message in state.messages:
            await conn.execute(
                f"INSERT INTO {self._t['workspace_message']} (tenant_id,user_id,message_id,request_id,record) VALUES ($1,$2,$3,$4,$5::jsonb)",
                tenant,
                user,
                message["id"],
                message["request_id"],
                json.dumps(message),
            )
        await save_reminders(conn, self._t, tenant, user, state)
        await save_tasks(conn, self._t, tenant, user, old, state)
        await save_proactive(conn, self._t, tenant, user, old, state)
        await save_execution(conn, self._t, tenant, user, old, state)
        from xuanpu_server.task_triggers.postgres import save as save_triggers

        await save_triggers(conn,self._t,tenant,user,old,state)
        for audit in state.audits:
            await conn.execute(
                f"INSERT INTO {self._t['audit']} (tenant_id,user_id,source,kind,at,actor,data) VALUES ($1,$2,'control',$3,$4,$5,$6::jsonb)",
                tenant,
                user,
                audit["kind"],
                audit["at"],
                audit["actor"],
                json.dumps(audit["data"]),
            )

    async def durable_candidates(self, *, after: tuple[str, str] | None = None,
                                 limit: int = 100, now: float) -> list[tuple[str, str, str]]:
        """持久due索引只读候选标识，不装载员工全部任务正文。"""
        if not 1 <= limit <= 100:
            raise ValueError('派发候选最多100项')
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT DISTINCT ON (d.tenant_id,d.user_id) d.tenant_id,d.user_id,d.task_id "
                f"FROM {self._t['task_dispatch']} d JOIN {self._t['work_task']} t USING(tenant_id,user_id,task_id) "
                "WHERE t.status IN ('accepted','running','waiting_user') AND "
                "((d.state IN ('ready','retry_wait','paused_shutdown') AND d.due_at<=$3) "
                "OR COALESCE(t.record->>'cancel_requested','false')='true') "
                "AND ($1::text IS NULL OR (d.tenant_id,d.user_id)>($1,$2)) "
                "ORDER BY d.tenant_id,d.user_id,COALESCE(t.record->>'cancel_requested','false') DESC,d.due_at,d.task_id LIMIT $4",
                *(after or (None,None)),now,limit)
        return [(row['tenant_id'], row['user_id'], row['task_id']) for row in rows]

    async def task_owners(self, after: tuple[str, str] | None = None,
                          limit: int = 100, *, legacy_only: bool = False) -> list[tuple[str, str]]:
        """内部恢复包含停用身份，记录数受每员工任务上限限制。"""
        from .postgres_work_tasks import PENDING_TASKS

        if not 1 <= limit <= 100:
            raise ValueError('分页数量须为1至100')
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT DISTINCT tenant_id,user_id FROM {self._t['work_task']} "
                "WHERE " + PENDING_TASKS + (" AND COALESCE(record->>'recovery_policy_version','1')='1'" if legacy_only else '') + " AND ($1::text IS NULL OR (tenant_id,user_id)>($1,$2)) "
                "ORDER BY tenant_id,user_id LIMIT $3", *(after or (None,None)), limit)
        return [(row['tenant_id'], row['user_id']) for row in rows]

    async def owners(
        self,
        after: tuple[str, str] | None = None,
        limit: int = 100,
    ) -> list[tuple[str, str]]:
        """合并已有元数据所有者，过滤停用身份并稳定分页。"""
        if not 1 <= limit <= 100:
            raise ValueError("分页数量须为1至100")
        sources = (
            "workspace_item",
            "workspace_settings",
            "workspace_message",
            "workspace_request",
            "proactive_check",
            "proactive_followup",
            "proactive_scan_state",
        )
        owners = " UNION ".join(
            f"SELECT tenant_id,user_id FROM {self._t[name]}" for name in sources
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT o.tenant_id,o.user_id FROM ({owners}) o "
                f"JOIN {self._t['tenant']} t ON t.tenant_id=o.tenant_id AND t.status='active' "
                f"JOIN {self._t['user']} u ON u.tenant_id=o.tenant_id AND u.user_id=o.user_id AND u.status='active' "
                "WHERE ($1::text IS NULL OR (o.tenant_id,o.user_id)>($1,$2)) "
                "ORDER BY o.tenant_id,o.user_id LIMIT $3",
                after[0] if after else None,
                after[1] if after else None,
                limit,
            )
        return [(row["tenant_id"], row["user_id"]) for row in rows]

    async def messages(
        self,
        tenant: str,
        user: str,
        after_id: int,
        limit: int,
        *,
        matter_id: str | None = None,
        before_id: int | None = None,
        latest: bool = False,
    ) -> list[dict[str, Any]]:
        """按员工复合索引增量读取，始终限定分页窗口。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT record FROM {self._t['workspace_message']} WHERE tenant_id=$1 AND user_id=$2 AND message_id>$3 AND ($5::text IS NULL OR record->>'matter_id'=$5) AND ($6::bigint IS NULL OR message_id<$6) ORDER BY message_id "
                + ("DESC" if latest else "ASC")
                + " LIMIT $4",
                tenant,
                user,
                after_id,
                limit,
                matter_id,
                before_id,
            )
        found = [json.loads(row["record"]) for row in rows]
        return list(reversed(found)) if latest else found

    async def find_message(self, tenant: str, user: str, request_id: str) -> dict[str, Any] | None:
        """按员工与请求唯一索引读取，避免扫描历史对话。"""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT record FROM {self._t['workspace_message']} WHERE tenant_id=$1 AND user_id=$2 AND request_id=$3",
                tenant,
                user,
                request_id,
            )
        return dict(json.loads(row["record"])) if row is not None else None

    async def pending_proposals(self, tenant: str, user: str) -> list[dict[str, Any]]:
        """部分索引筛建议，终态由员工请求唯一索引反连接。"""
        table = self._t["workspace_message"]
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT m.record FROM {table} m WHERE m.tenant_id=$1 AND m.user_id=$2 "
                "AND m.record->>'role'='assistant' AND right(m.request_id,10)=':assistant' "
                "AND (jsonb_typeof(m.record#>'{metadata,route,workspace_draft}')='object' "
                "OR jsonb_typeof(m.record#>'{metadata,route,reminder_draft}')='object') "
                f"AND NOT EXISTS (SELECT 1 FROM {table} terminal "
                "WHERE terminal.tenant_id=m.tenant_id AND terminal.user_id=m.user_id "
                "AND terminal.request_id IN (left(m.request_id,length(m.request_id)-10)||':confirmed', "
                "left(m.request_id,length(m.request_id)-10)||':cancelled')) "
                f"AND NOT EXISTS (SELECT 1 FROM {self._t['workspace_request']} decision "
                "WHERE decision.tenant_id=m.tenant_id AND decision.user_id=m.user_id "
                "AND decision.request_id='proposal:'||left(m.request_id,length(m.request_id)-10) "
                "AND decision.result->>'state' IN ('confirmed','cancelled')) "
                "ORDER BY m.message_id DESC LIMIT 100",
                tenant,
                user,
            )
        return [json.loads(row["record"]) for row in rows]

    async def proposal_result(
        self, tenant: str, user: str, request_id: str
    ) -> dict[str, Any] | None:
        """由本人请求唯一索引读取权威回执，未完成预留不算终态。"""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT result FROM {self._t['workspace_request']} WHERE tenant_id=$1 AND user_id=$2 AND request_id=$3 AND result->>'state' IN ('confirmed','cancelled')",
                tenant,
                user,
                "proposal:" + request_id,
            )
        return dict(json.loads(row["result"])) if row is not None else None
