"""人物与关系 PostgreSQL 真实库迁移（0033）与持久仓储契约测试。"""

from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING

import asyncpg
import pytest
from taifeng_stores import SqlNamespace

from server.core.controlplane import AuditQuery
from server.core.controlplane.postgres import PostgresControlPlane
from server.core.controlplane.schema import apply_migrations, ensure_current, latest_version
from server.core.objectstore.local import LocalObjectStore
from server.core.tenancy import Principal, Tenant
from server.domains.identity import UserRecord
from server.domains.identity.postgres import PostgresIdentityStore
from server.domains.people.models import MergeInput, UnmergeInput
from server.domains.people.service import PeopleService
from server.domains.workspace.service import WorkspaceService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture
async def pg_people(
    postgres_dsn: str, tmp_path: Path
) -> AsyncIterator[tuple[PostgresControlPlane, WorkspaceService, PeopleService, Principal, Principal]]:
    """在独占 schema 上执行全部迁移（至 0033）并装配真实 Postgres 人物服务。"""
    pool = await asyncpg.create_pool(postgres_dsn, min_size=1, max_size=4)
    namespace = SqlNamespace(schema="t_" + secrets.token_hex(6), prefix="xp_")
    try:
        await apply_migrations(pool, namespace)
        await ensure_current(pool, namespace)
        control = PostgresControlPlane(pool, namespace=namespace)
        identity = PostgresIdentityStore(pool, namespace=namespace)
        objects = LocalObjectStore(tmp_path / "objects")
        workspace = WorkspaceService(
            store=control.workspace_store, control=control, identity=identity, objects=objects
        )
        people = PeopleService(store=control.people_store, workspace=workspace, objects=objects)
        await control.create_tenant(
            Tenant(tenant_id="acme", name="Acme", status="active", created_at=time.time())
        )
        for uid in ("usr_a", "usr_b"):
            await identity.create_user(
                UserRecord(tenant_id="acme", user_id=uid, display_name=uid, created_at=time.time())
            )
        alice = Principal(key_id=None, role="member", tenant_id="acme", user_id="usr_a")
        bob = Principal(key_id=None, role="member", tenant_id="acme", user_id="usr_b")
        yield control, workspace, people, alice, bob
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{namespace.schema}" CASCADE')
        await pool.close()


async def test_postgres_people_full_lifecycle(
    pg_people: tuple[PostgresControlPlane, WorkspaceService, PeopleService, Principal, Principal],
) -> None:
    """真实 PostgreSQL 上验证 0033/0034 建表、公司实体、会议记录、跨实体多跳图谱、头像、合并/撤销、隔离与脱敏审计。"""
    assert latest_version() == 34
    control, workspace, people, alice, bob = pg_people

    # 新建两个人物与一个事项
    p1 = await people.create_person(
        alice,
        {
            "name": "王建国",
            "aliases": ["老王"],
            "birth": {"year": 1986, "month": 5, "day": 12, "calendar": "solar"},
            "organization": "星辰科技",
            "contacts": [{"kind": "phone", "value": "13800138000", "primary": True}],
            "tags": ["供应链"],
            "extra": {"alma_mater": "浙江大学"},
        },
        "pg-p1",
    )
    p2 = await people.create_person(alice, {"name": "李四", "organization": "云帆"}, "pg-p2")
    matter = await workspace.create(alice, "matter", {"title": "供应链升级"}, "pg-m1")

    # 幂等重放
    again = await people.create_person(
        alice,
        {
            "name": "王建国",
            "aliases": ["老王"],
            "birth": {"year": 1986, "month": 5, "day": 12, "calendar": "solar"},
            "organization": "星辰科技",
            "contacts": [{"kind": "phone", "value": "13800138000", "primary": True}],
            "tags": ["供应链"],
            "extra": {"alma_mater": "浙江大学"},
        },
        "pg-p1",
    )
    assert again["id"] == p1["id"]

    # 上传并读取头像
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    p1 = await people.upload_avatar(alice, p1["id"], png, p1["version"])
    assert p1["has_avatar"] is True
    raw, mime = await people.get_avatar(alice, p1["id"])
    assert raw == png and mime == "image/png"

    # 建关系（人↔人、人↔事项）与笔记
    await people.create_relation(
        alice,
        {"from_person_id": p2["id"], "target_kind": "person", "target_id": p1["id"], "relation_type": "introduced_by"},
        "pg-rel-1",
    )
    await people.create_relation(
        alice,
        {
            "from_person_id": p1["id"],
            "target_kind": "matter",
            "target_id": matter["id"],
            "relation_type": "matter_role",
            "role": "负责人",
        },
        "pg-rel-2",
    )
    note = await people.create_note(
        alice,
        {"content": "与王建国沟通供应链方案", "person_ids": [p1["id"]], "matter_id": matter["id"]},
        "pg-note-1",
    )
    assert note["person_ids"] == [p1["id"]]

    # 事项反查与 2 跳关系图
    linked = await people.list_matter_people(alice, matter["id"])
    assert [item["person"]["id"] for item in linked] == [p1["id"]]
    graph = await people.graph(alice, p2["id"], depth=2)
    assert {n["id"] for n in graph["nodes"]} >= {p1["id"], p2["id"], matter["id"]}

    # 0034：真实 PG 上验证公司一等实体、org→org 与 person→org 关系、会议记录与跨实体多跳穿透
    parent_org = await people.create_org(
        alice, {"name": "中国电信股份有限公司上海分公司", "aliases": ["上海电信"], "city": "上海"}, "pg-org-1"
    )
    sub_org = await people.create_org(
        alice, {"name": "上海理想信息产业（集团）有限公司", "aliases": ["上海理想"], "city": "上海"}, "pg-org-2"
    )
    await people.create_relation(
        alice,
        {
            "from_kind": "org",
            "from_id": sub_org["id"],
            "target_kind": "org",
            "target_id": parent_org["id"],
            "relation_type": "subsidiary_of",
        },
        "pg-org-rel-1",
    )
    await people.create_relation(
        alice,
        {
            "from_kind": "person",
            "from_id": p1["id"],
            "target_kind": "org",
            "target_id": sub_org["id"],
            "relation_type": "works_at",
        },
        "pg-org-rel-2",
    )
    meeting = await people.create_note(
        alice,
        {
            "record_kind": "meeting",
            "title": "PG 会议纪要",
            "location": "半岛酒店 201",
            "content": "与王建国讨论供应链升级。",
            "summary": "明确方案时间表",
            "decisions": ["下周三交付方案"],
            "org_ids": [parent_org["id"], sub_org["id"]],
            "person_ids": [p1["id"]],
            "matter_ids": [matter["id"]],
        },
        "pg-meeting-1",
    )
    assert meeting["record_kind"] == "meeting" and meeting["decisions"] == ["下周三交付方案"]
    org_graph = await people.entity_graph(alice, "org", parent_org["id"], depth=2)
    assert {n["id"] for n in org_graph["nodes"]} >= {parent_org["id"], sub_org["id"], p1["id"]}
    assert any(r["id"] == meeting["id"] for r in org_graph["records"])

    # 软合并与撤销合并（验证自引用外键顺序在 PG 上完全通过）
    merged = await people.merge_people(
        alice,
        p1["id"],
        MergeInput(
            target_id=p2["id"],
            source_version=p1["version"],
            target_version=p2["version"],
            request_id="pg-merge",
        ),
    )
    assert merged["source"]["merged_into_id"] == p2["id"]
    assert merged["target"]["has_avatar"] is True

    unmerged = await people.unmerge_people(
        alice,
        p1["id"],
        UnmergeInput(
            source_version=merged["source"]["version"],
            target_version=merged["target"]["version"],
            request_id="pg-unmerge",
        ),
    )
    assert unmerged["source"]["merged_into_id"] is None
    assert unmerged["target"]["has_avatar"] is False

    # 跨员工强隔离
    assert await people.list_people(bob) == []

    # 脱敏审计落库验证
    audits = await control.query_audit("acme", AuditQuery())
    people_audits = [a for a in audits if a.kind.startswith("people.")]
    assert people_audits
    assert "王建国" not in str([a.data for a in people_audits])
    assert "13800138000" not in str([a.data for a in people_audits])
