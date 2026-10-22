"""普通报销原件分块：只读当前授权的指定快照证据，不产生查验事实。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.funding.materials import read_original

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.objects import OriginalObjects
    from xuanpu_finance.storage import FinanceStore


async def read_material(
    store: FinanceStore, objects: OriginalObjects, who: FinancePrincipal,
    document_id: str, receipt_id: str, offset: int, limit: int,
    document_version: int | None = None,
) -> dict[str, Any]:
    """人工作为当前申请人、财务或有效分派人，方可核对本单当前或历史原件。"""
    if who.origin != 'human':
        raise FinanceError('HUMAN_REQUIRED', 403, '仅实际员工可读取原件材料')
    if offset < 0 or not 1 <= limit <= 262144:
        raise FinanceError('MATERIAL_RANGE_INVALID', 422, '原件读取范围无效')
    async with store.pool.acquire() as conn, conn.transaction():
        doc = await store._expense_record_row(conn, who, document_id)
        snapshot = await store._expense_material_snapshot(conn, doc, document_version)
        if not await store._expense_material_linked(conn, doc, snapshot, receipt_id):
            raise FinanceError('RESOURCE_NOT_FOUND', 404, '原件当前不可见')
        return await read_original(
            conn, objects, who.tenant_id, doc['legal_entity_id'], document_id,
            snapshot['version'], receipt_id, offset, limit, awaiting_original=True,
        )
