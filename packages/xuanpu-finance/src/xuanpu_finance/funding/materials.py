"""实际人工核对从独立加密原件读取受权固定分块，不依赖原平台文件仍在线。"""
from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.common import decode

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.objects import OriginalObjects
    from xuanpu_finance.storage import FinanceStore
    from xuanpu_finance.storage.business_base import Connection


async def read_material(store: FinanceStore,objects: OriginalObjects,who: FinancePrincipal,
                        document_id: str,receipt_id: str,offset: int,limit: int) -> dict[str,Any]:
    """每块重验当前单据读权和完整原件摘要，不返回对象键、平台令牌或他人材料。"""
    if offset<0 or not 1<=limit<=262144:
        raise FinanceError('MATERIAL_RANGE_INVALID',422,'原件读取范围无效')
    async with store.pool.acquire() as conn,conn.transaction():
        doc=await store._funding_row(conn,who,document_id)
        allowed={r['id'] for r in await store._claim_evidence(conn,doc)}
        if receipt_id not in allowed:
            raise FinanceError('RESOURCE_NOT_FOUND',404,'原件当前不可见')
        return await read_original(conn,objects,who.tenant_id,doc['legal_entity_id'],document_id,
                                   doc['version'],receipt_id,offset,limit)


async def read_original(
    conn: Connection, objects: OriginalObjects, tenant: str, entity: str,
    document_id: str, version: int, receipt_id: str, offset: int, limit: int,
    *, awaiting_original: bool = False,
) -> dict[str, Any]:
    """复核独立收件责任、文件引用、实际大小和字节摘要，返回固定分块且不改变解析态。"""
    row=await conn.fetchrow("""SELECT r.object_key,r.sha256,r.file_ref,
      i.actual_sha256,i.size_bytes,i.content_type,i.state,i.object_key AS item_object_key,
      i.receipt_id,i.file_ref AS item_file_ref FROM finance_receipt r JOIN finance_item i
      ON(i.tenant_id=r.tenant_id AND i.legal_entity_id=r.legal_entity_id
      AND i.operation_id=r.operation_id AND i.position=r.position)
      WHERE r.tenant_id=$1 AND r.legal_entity_id=$2 AND r.id=$3""",tenant,entity,receipt_id)
    states={'material_received','parsed','duplicate_linked'}
    if awaiting_original:
        states.add('awaiting_original')
    if row is None or row['state'] not in states or row['size_bytes'] is None or not 0<row['size_bytes']<=50*1024*1024:
        raise FinanceError('MATERIAL_SOURCE_INVALID',409,'原件接收责任不完整，不能用于人工核对')
    if offset>=row['size_bytes']:
        raise FinanceError('MATERIAL_RANGE_INVALID',422,'读取位置超出原件范围')
    if row['item_object_key'] != row['object_key'] or row['receipt_id'] != receipt_id or decode(row['item_file_ref']) != decode(row['file_ref']):
        raise FinanceError('MATERIAL_SOURCE_INVALID',409,'原件独立收件责任与回执不符')
    raw=await objects.get(row['object_key'])
    if len(raw)!=row['size_bytes'] or hashlib.sha256(raw).hexdigest()!=row['sha256'] or row['sha256']!=row['actual_sha256'] or decode(row['file_ref'])['sha256']!=row['sha256']:
        raise FinanceError('MATERIAL_SOURCE_INVALID',409,'原件字节、大小或固定摘要不符')
    end=min(offset+limit,len(raw))
    return {'document_id':document_id,'document_version':version,'receipt_id':receipt_id,
            'sha256':row['sha256'],'media_type':row['content_type'],'size':len(raw),'offset':offset,
            'content_base64':base64.b64encode(raw[offset:end]).decode(),'next_offset':end if end<len(raw) else None}
