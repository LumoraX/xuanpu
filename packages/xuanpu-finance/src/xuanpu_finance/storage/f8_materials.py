"""本人支持材料的只读目录和实际收件元数据，不制造验真或执行权。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode, encode_cursor, page, timestamp_cursor
from xuanpu_finance.storage.f7_records import RecordStore

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection

MATERIAL_ITEMS = """SELECT i.*,r.id AS actual_receipt,r.owner_id AS receipt_owner,
 r.object_key AS receipt_object,r.sha256 AS receipt_sha,r.file_ref AS receipt_file
 FROM finance_item i LEFT JOIN finance_receipt r
 ON(r.tenant_id=i.tenant_id AND r.legal_entity_id=i.legal_entity_id
 AND r.operation_id=i.operation_id AND r.position=i.position AND r.id=i.receipt_id)
 WHERE i.tenant_id=$1 AND i.legal_entity_id=$2 AND i.operation_id=$3 ORDER BY i.position"""


def _selectable(item: Any, operation: Any, simulation: bool) -> bool:
    """收件项与真实回执必须构成一致责任链，不能依赖操作总体成功。"""
    if item['state'] != 'material_received' or item['actual_receipt'] is None:
        return False
    file_ref = decode(item['file_ref'])
    receipt_file = decode(item['receipt_file'])
    ownership = (item['receipt_owner'] == operation['owner_id']
                 and operation['source_simulation'] == simulation)
    objects = (bool(item['object_key']) and item['object_key'] == item['receipt_object']
               and bool(item['size_bytes']) and item['size_bytes'] > 0 and bool(item['content_type']))
    digest = item['receipt_sha']
    integrity = (len(digest) == 64 and digest == item['actual_sha256'] == file_ref.get('sha256')
                 and file_ref == receipt_file)
    return bool(ownership and objects and integrity)


def _material_view(item: Any, operation: Any, simulation: bool) -> dict[str, Any]:
    """仅显示实际逐项状态和收件元数据，不暴露对象地址或平台文件引用。"""
    return {
        'position': item['position'], 'state': item['state'], 'receipt_id': item['receipt_id'],
        'filename': item['filename'], 'size_bytes': item['size_bytes'],
        'media_type': item['content_type'], 'sha256': item['actual_sha256'],
        'verification_state': 'not_checked', 'selectable': _selectable(item, operation, simulation),
    }


class MaterialStore(RecordStore):
    """支持材料目录严格仅本人可见，当前reviewer不拥有他人目录读取特权。"""

    async def _supporting_material_operation(
        self, conn: Connection, who: FinancePrincipal, id: str,
    ) -> Any:
        """每次先核当前租户、用途和实际owner，再核公司授权。"""
        row = await conn.fetchrow(
            "SELECT * FROM finance_operation WHERE tenant_id=$1 AND owner_id=$2 AND id=$3 AND purpose='supporting_material'",
            who.tenant_id, who.user_id, id,
        )
        if row is None:
            raise FinanceError('RESOURCE_NOT_FOUND', 404, '支持材料导入不存在')
        await self._role(conn, who, row['legal_entity_id'])
        return row

    async def list_supporting_material_imports(
        self, who: FinancePrincipal, entity: str, cursor: str | None = None, limit: int = 20,
    ) -> dict[str, Any]:
        """在同一快照统计本人操作并按时间、标识倒序续读，摘要实时计算。"""
        async with self.pool.acquire() as conn, conn.transaction(isolation='repeatable_read'):
            await self._role(conn, who, entity)
            scope = request_digest([who.tenant_id, who.user_id, entity, 'supporting_material_imports', 'desc'])
            stamp, last = timestamp_cursor(cursor, scope, limit)
            where = "tenant_id=$1 AND owner_id=$2 AND legal_entity_id=$3 AND purpose='supporting_material'"
            args = (who.tenant_id, who.user_id, entity)
            total = await conn.fetchval('SELECT count(*) FROM finance_operation WHERE ' + where, *args)
            rows = await conn.fetch(
                'SELECT * FROM finance_operation WHERE ' + where
                + ' AND ($4::timestamptz IS NULL OR (created_at,id)<($4,$5::text))'
                + ' ORDER BY created_at DESC,id DESC LIMIT $6', *args, stamp, last, limit + 1,
            )
            visible = rows[:limit]
            items = []
            for row in visible:
                snapshot = await self._snapshot(conn, row)
                items.append({'id': row['id'], 'version': row['version'], 'status': row['status'],
                              'created_at': row['created_at'].isoformat(),
                              'total': snapshot['summary']['total'], 'summary': snapshot['summary']})
            after = [visible[-1]['created_at'].isoformat(), visible[-1]['id']] if len(rows) > limit else None
            return page(items, total, encode_cursor(scope, after) if after else None)

    async def get_supporting_material_import(
        self, who: FinancePrincipal, id: str,
    ) -> dict[str, Any]:
        """同一快照读取兼容操作结构与真实材料责任索引，不访问对象服务。"""
        async with self.pool.acquire() as conn, conn.transaction(isolation='repeatable_read'):
            operation = await self._supporting_material_operation(conn, who, id)
            items = await conn.fetch(MATERIAL_ITEMS, who.tenant_id, operation['legal_entity_id'], id)
            simulation = await self._simulation(conn)
            return {'operation': await self._snapshot(conn, operation),
                    'materials': [_material_view(item, operation, simulation) for item in items]}
