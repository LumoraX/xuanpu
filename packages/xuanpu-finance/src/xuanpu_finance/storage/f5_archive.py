"""专用资金家庭的实际支持原件、原授权和核销/退回责任闭包。"""
from __future__ import annotations

import base64
import hashlib
import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.f3_evidence_closure import serialize_record

if TYPE_CHECKING:
    from xuanpu_finance.storage.business_base import Connection


async def funding_family(conn: Connection,tenant: str,entity: str,ids: list[str]) -> list[str]:
    """同公司原预付和收尾双向只读闭包，历史子单不因终态消失。"""
    rows=await conn.fetch("""WITH RECURSIVE links(a,b) AS (
      SELECT document_id,source_case_id FROM finance_funding_case WHERE tenant_id=$1 AND legal_entity_id=$2 AND source_case_id IS NOT NULL
      UNION SELECT source_case_id,document_id FROM finance_funding_case WHERE tenant_id=$1 AND legal_entity_id=$2 AND source_case_id IS NOT NULL
    ), family(id) AS (SELECT unnest($3::text[]) UNION SELECT l.b FROM links l JOIN family f ON(l.a=f.id))
    SELECT id FROM family ORDER BY id""",tenant,entity,ids)
    return [r['id'] for r in rows]


def funding_artifact(kind: str,rows: list[Any]) -> dict[str,Any]:
    """派生责任记录保留独立源，不把内部人工核对冒充银行事实。"""
    records=[]
    for row in rows:
        records.append({k:format(v,'.2f') if isinstance(v,Decimal) else v for k,v in serialize_record(row).items()})
    raw=json.dumps({'source':'derived','data':records},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    return {'id':kind,'path':'funding/'+kind+'.json','kind':kind,'sha256':hashlib.sha256(raw).hexdigest(),'media_type':'application/json','size':len(raw),'source':{'content_base64':base64.b64encode(raw).decode()}}


async def funding_archive_sources(conn: Connection,tenant: str,entity: str,ids: list[str],simulation: bool) -> tuple[list[dict[str,Any]],list[str]]:
    """归档实际责任链和全部历史材料，固定字节的读取由既有pipeline完成。"""
    cases=await conn.fetch('SELECT * FROM finance_funding_case WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=ANY($3::text[]) ORDER BY document_id',tenant,entity,ids)
    if not cases:
        return [],[]
    doc_ids=[r['document_id'] for r in cases]
    authority_ids=sorted({r['authority_id'] for r in cases if r['authority_id'] is not None})
    histories=await conn.fetch('SELECT * FROM finance_expense_history WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=ANY($3::text[]) ORDER BY id,version',tenant,entity,doc_ids)
    authorities=await conn.fetch("SELECT a.* FROM finance_funding_authority a WHERE a.tenant_id=$1 AND a.legal_entity_id=$2 AND (a.id=ANY($3::text[]) OR EXISTS(SELECT 1 FROM finance_expense_history h WHERE h.tenant_id=a.tenant_id AND h.legal_entity_id=a.legal_entity_id AND h.id=ANY($4::text[]) AND h.data#>>'{funding,authority,id}'=a.id)) ORDER BY a.id",tenant,entity,authority_ids,doc_ids)
    authority_ids=[a['id'] for a in authorities]
    decisions=await conn.fetch('SELECT * FROM finance_funding_authority_decision WHERE tenant_id=$1 AND legal_entity_id=$2 AND authority_id=ANY($3::text[]) ORDER BY created_at,id',tenant,entity,authority_ids)
    reservations=await conn.fetch('SELECT * FROM finance_funding_reservation WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=ANY($3::text[]) ORDER BY document_id',tenant,entity,doc_ids)
    artifacts=[funding_artifact('funding_history',list(histories)),funding_artifact('funding_cases',list(cases)),funding_artifact('funding_authorities',list(authorities)),funding_artifact('funding_authority_decisions',list(decisions)),funding_artifact('funding_reconciliations',list(reservations))]
    rows=await conn.fetch("""SELECT DISTINCT r.id,r.object_key,r.sha256,i.content_type,i.size_bytes,i.state,i.actual_sha256,o.source_simulation,r.file_ref
      FROM finance_expense_evidence e JOIN finance_receipt r ON(r.tenant_id=e.tenant_id AND r.legal_entity_id=e.legal_entity_id AND r.id=e.receipt_id)
      JOIN finance_operation o ON(o.tenant_id=r.tenant_id AND o.legal_entity_id=r.legal_entity_id AND o.id=r.operation_id)
      JOIN finance_item i ON(i.tenant_id=r.tenant_id AND i.legal_entity_id=r.legal_entity_id AND i.operation_id=r.operation_id AND i.position=r.position)
      WHERE e.tenant_id=$1 AND e.legal_entity_id=$2 AND e.document_id=ANY($3::text[]) ORDER BY r.id""",tenant,entity,doc_ids)
    if not rows:
        raise FinanceError('ARCHIVE_FUNDING_MATERIAL_REQUIRED',409,'资金归档缺少实际支持原件')
    for row in rows:
        if (row['source_simulation']!=simulation or row['state'] not in {'material_received','parsed','duplicate_linked'}
            or row['size_bytes'] is None or row['size_bytes']<=0 or not row['object_key'] or not row['content_type']
            or row['sha256']!=row['actual_sha256'] or json.loads(row['file_ref'])['sha256']!=row['sha256']):
            raise FinanceError('ARCHIVE_FUNDING_MATERIAL_INVALID',409,'资金实际材料责任链、固定摘要或来源环境不符')
        artifacts.append({'id':'funding_material_'+row['id'],'path':'funding/materials/'+row['id']+'.bin','kind':'funding_material','sha256':row['sha256'],'media_type':row['content_type'],'size':row['size_bytes'],'source':{'object_key':row['object_key']}})
    return artifacts,['funding_history','funding_material','funding_cases','funding_authorities','funding_authority_decisions','funding_reconciliations']
