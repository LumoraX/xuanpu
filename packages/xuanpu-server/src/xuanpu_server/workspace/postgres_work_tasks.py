"""员工事务内加载和保存任务；新增任务、输入引用和事件一起提交。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

PENDING_TASKS = ("(status IN ('accepted','running','waiting_user') "
    "OR record->>'resources_pending'='true' OR record->>'artifact_recovery_pending'='true' "
    "OR (record->>'error_code'='container_cleanup_unverified' "
    "AND COALESCE(record->>'resources_recovered','false')='false'))")

if TYPE_CHECKING:
    from xuanpu_server.workspace.models import WorkspaceState


async def load_tasks(
    conn: Any, tables: dict[str, str], tenant: str, user: str, state: WorkspaceState,
    request_id: str | None,
) -> None:
    """普通工作区操作不加载任务正文；任务事务才读取有界任务索引。"""
    if request_id is None or not request_id.startswith("work-task:"):
        return
    pending = " AND " + PENDING_TASKS if request_id == "work-task:pending" else ""
    rows = await conn.fetch(
        f"SELECT task_id,record FROM {tables['work_task']} WHERE tenant_id=$1 AND user_id=$2" + pending,
        tenant, user,
    )
    state.work_tasks = {row["task_id"]: json.loads(row["record"]) for row in rows}
    if request_id.startswith("work-task:events:"):
        task_id = request_id.removeprefix("work-task:events:")
        rows = await conn.fetch(
            f"SELECT event_id,data FROM {tables['work_task_event']} "
            "WHERE tenant_id=$1 AND user_id=$2 AND task_id=$3 ORDER BY event_id",
            tenant, user, task_id,
        )
        state.work_task_events = [{**json.loads(row["data"]), "event_id": row["event_id"]}
                                  for row in rows]


async def save_tasks(
    conn: Any, tables: dict[str, str], tenant: str, user: str, old: WorkspaceState,
    state: WorkspaceState,
) -> None:
    """先写任务，再固定输入和追加事件；任何异常回滚整个员工事务。"""
    for task_id, record in state.work_tasks.items():
        if old.work_tasks.get(task_id) == record:
            continue
        await conn.execute(
            f"INSERT INTO {tables['work_task']} "
            "(tenant_id,user_id,task_id,request_id,input_fingerprint,status,state_version,"
            "process_owner,accepted_at,updated_at,finished_at,record) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb) "
            "ON CONFLICT (tenant_id,user_id,task_id) DO UPDATE SET "
            "status=EXCLUDED.status,state_version=EXCLUDED.state_version,process_owner=EXCLUDED.process_owner,"
            "updated_at=EXCLUDED.updated_at,finished_at=EXCLUDED.finished_at,record=EXCLUDED.record",
            tenant, user, task_id, record["request_id"], record["input_fingerprint"], record["status"],
            record["state_version"], record["process_owner"], record["accepted_at"],
            record["updated_at"], record["finished_at"], json.dumps(record),
        )
        if task_id not in old.work_tasks:
            for entry in record["inputs"]:
                await conn.execute(
                    f"INSERT INTO {tables['work_task_input']} "
                    "(tenant_id,user_id,task_id,item_id,revision,record) VALUES ($1,$2,$3,$4,$5,$6::jsonb)",
                    tenant, user, task_id, entry["item_id"], entry["revision"], json.dumps(entry),
                )
    for event in state.work_task_events[len(old.work_task_events):]:
        await conn.execute(
            f"INSERT INTO {tables['work_task_event']} "
            "(tenant_id,user_id,task_id,state_version,at,event_kind,data) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)",
            tenant, user, event["task_id"], event["state_version"], event["at"], event["event_kind"],
            json.dumps({key: value for key, value in event.items() if key != "event_id"}),
        )
