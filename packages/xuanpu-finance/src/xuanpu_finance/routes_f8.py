"""支持材料本人目录及真实元数据的独立签名只读入口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from xuanpu_finance.dependencies import FinanceCaller, get_store
from xuanpu_finance.routes import Company, Cursor, Limit  # noqa: TC001 — FastAPI运行时解析

router = APIRouter()


@router.get('/v1/finance/supporting-material-imports', operation_id='listSupportingMaterialImports')
async def material_imports(
    request: Request, who: FinanceCaller, legal_entity_id: Company,
    cursor: Cursor = None, limit: Limit = 20,
) -> dict[str, Any]:
    """当前公司只列本人支持材料操作，实时授权与摘要共用持久存储。"""
    return await get_store(request).list_supporting_material_imports(who, legal_entity_id, cursor, limit)


@router.get('/v1/finance/supporting-material-imports/{id}', operation_id='getSupportingMaterialImport')
async def material_import(request: Request, id: str, who: FinanceCaller) -> dict[str, Any]:
    """只读收件元数据，agent也不能由此取得原字节、验真或业务执行权。"""
    return await get_store(request).get_supporting_material_import(who, id)
