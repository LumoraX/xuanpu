"""F10隔离恢复的真实CLI配置、维护入口与持久事实取证。"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import TYPE_CHECKING, Any

import asyncpg
import yaml
from xuanpu_finance.config import FinanceConfig, GrantConfig
from xuanpu_finance.expenses.models import ExpensePolicy

from .test_process import process_config
from .test_storage import WHO

if TYPE_CHECKING:
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from xuanpu_finance.maintenance import MaintenanceOptions


def recovery_config(directory: Path, dsn: str, gateway: str,
                    key: Ed25519PrivateKey) -> tuple[Path, FinanceConfig]:
    """使用真实两级审批配置，恢复时除位置和mode之外保持完全一致。"""
    path, config = process_config(directory, dsn, gateway, key)
    config.profile, config.synthetic = 'internal_records', False
    config.grants += [GrantConfig(tenant_id=WHO.tenant_id, user_id=user,
        legal_entity_id='company-a', role=role)
        for user, role in [('manager', 'approver'), ('finance-reviewer', 'reviewer')]]
    config.expense_policies = [ExpensePolicy(tenant_id=WHO.tenant_id,
        legal_entity_id='company-a', version=1, enabled=True,
        record_mode='internal_record', require_verified_invoice=False,
        categories=[{'id': 'travel', 'name': '交通'}], cost_centers=[],
        steps=[{'id': user, 'name': user, 'approver_user_id': user}
               for user in ['manager', 'finance-reviewer']],
        exception_owner_user_id='finance-reviewer')]
    config = FinanceConfig.model_validate(config.model_dump())
    write_config(path, config)
    return path, config


def write_config(path: Path, config: FinanceConfig) -> None:
    """仅写入测试夹具私有目录，配置本身不包含秘密值。"""
    path.write_text(yaml.safe_dump(config.model_dump(mode='json')))
    path.chmod(0o600)


async def maintenance_cli(action: str, options: MaintenanceOptions, package: Path,
                          tool: Path) -> dict[str, Any]:
    """调用产品实际维护CLI及成熟PG工具，不替代dump/restore。"""
    process = await asyncio.create_subprocess_exec(sys.executable, '-m',
        'xuanpu_finance.maintenance_cli', action, '--database-dsn-file',
        str(options.database_dsn_file), '--objects', str(options.object_root),
        '--key-file', str(options.key_file), '--package', str(package),
        '--pg-tool', str(tool), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    output, error = await asyncio.wait_for(process.communicate(), 60)
    assert process.returncode == 0, (output.decode(), error.decode())
    result: dict[str, Any] = json.loads(output)
    assert result['status'] == 'completed'
    return result


async def persisted_snapshot(dsn: str) -> dict[str, Any]:
    """只读快照比较业务历史、公司制度和授权代际，不输出连接秘密。"""
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction(isolation='repeatable_read', readonly=True):
            tables = ['finance_company', 'finance_grant', 'finance_grant_epoch',
                'finance_expense_policy', 'finance_expense_policy_revision',
                'finance_expense_claim', 'finance_expense_history', 'finance_workflow',
                'finance_workflow_step', 'finance_decision', 'finance_receipt']
            return {table: sorted(json.loads(await conn.fetchval(
                'SELECT COALESCE(jsonb_agg(to_jsonb(t)),\'[]\'::jsonb)::text FROM '
                + table + ' t')), key=lambda row: json.dumps(row, sort_keys=True)) for table in tables}
    finally:
        await conn.close()
