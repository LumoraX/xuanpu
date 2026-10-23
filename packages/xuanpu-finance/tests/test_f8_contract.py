"""F8发布目录、逐项材料元数据及模型读取边界必须同步。"""
from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_finance.storage import FinanceStore

from .test_package import PACKAGE, contracts, operations, validate_schema


def test_materials_contract_and_skill_read_tools() -> None:
    """只发布两项本人元数据读取，不把原件读取或人工执行开放给模型。"""
    document = contracts()['finance']
    declared = operations(document)
    assert document['info']['version'] == '0.12.0-f11'
    assert declared['listSupportingMaterialImports'][:2] == ('/v1/finance/supporting-material-imports','get')
    assert declared['getSupportingMaterialImport'][:2] == ('/v1/finance/supporting-material-imports/{id}','get')
    validate_schema('SupportingMaterialImportPage',{'items':[],'total':0,'next_cursor':None})
    for name in ['invoice-intake','expense-reimbursement']:
        path = PACKAGE/'skills'/name/'SKILL.md'
        frontmatter = yaml.safe_load(path.read_text().split('---',2)[1])
        assert 'api__finance__listSupportingMaterialImports' in frontmatter['tool_names']
        assert 'api__finance__getSupportingMaterialImport' in frontmatter['tool_names']
        assert 'not_checked' in path.read_text()
        assert 'api__finance__getExpenseMaterial' not in frontmatter['tool_names']


async def test_real_material_responses_match_published_schema(store: FinanceStore, tmp_path: Path) -> None:
    """实际PG收件的列表与详情都通过已发布schema，元数据不会虚构。"""
    from .test_f8_materials import completed
    from .test_storage import WHO

    op, _ = await completed(store,tmp_path)
    validate_schema('SupportingMaterialImportPage',await store.list_supporting_material_imports(WHO,'company-a'))
    validate_schema('SupportingMaterialImportDetail',await store.get_supporting_material_import(WHO,op['id']))
