"""姓名更新作为协调事务的待提交写集，不能另开身份写事务。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from xuanpu_server.errors import ConflictError, PermissionDeniedError

if TYPE_CHECKING:
    import asyncpg
    from asyncpg.pool import PoolConnectionProxy
    from taifeng_stores import SqlNamespace

    from xuanpu_server.coordination.models import CoordinationTransaction

    Connection = asyncpg.Connection | PoolConnectionProxy


class ProfileUpdate(BaseModel):
    """仅允许改姓名，外部目录不能借此改启用状态或身份角色。"""
    model_config = ConfigDict(frozen=True, extra='forbid')
    tenant_id: str
    user_id: str
    expected_epoch: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    expected_name: str
    display_name: str = Field(min_length=1, max_length=256)
    source: str


def validate_profiles(tx: CoordinationTransaction) -> None:
    """源权威、目标锁和身份代次必须与当前工作单元一致。"""
    authority = next((a for a in tx.organization.authority.values()
                      if a.object_type == 'user' and a.field == 'display_name'), None)
    source = authority.source if authority else 'local'
    for update in tx.profile_updates.values():
        if update.tenant_id != tx.tenant_id or update.source != source:
            raise ConflictError('员工姓名的字段权威已改变')
        if update.user_id not in tx.participants or tx.participant_epochs[update.user_id] != update.expected_epoch:
            raise PermissionDeniedError('员工身份权限版本已改变')


async def save_profiles(conn: Connection, namespace: SqlNamespace, tx: CoordinationTransaction) -> None:
    """使用同一数据库连接提交CAS姓名变更，不修改身份或管理员角色。"""
    for update in tx.profile_updates.values():
        changed = await conn.fetchval(
            f"UPDATE {namespace.table('user')} SET display_name=$3,profile_version=profile_version+1 "
            "WHERE tenant_id=$1 AND user_id=$2 AND status='active' AND lifecycle_epoch=$4 "
            "AND profile_version=$5 AND display_name=$6 RETURNING user_id",
            update.tenant_id, update.user_id, update.display_name, update.expected_epoch,
            update.expected_version, update.expected_name)
        if changed is None:
            raise ConflictError('员工资料已改变，请重新预览')
