"""能力结果统计的 CSV 导出（R4-F1）：与页面同一权限、同一口径与字段白名单。

- 员工导出本人能力任务的元数据（任务、能力修订、发布、状态、时间、已结算 token），不含正文或文件名；
- 管理员导出按能力与修订的计数与用量，不含任务、员工或正文；
- 未知不填 0：没有用量记录的任务 token 列为空；没有价格表时费用状态写 ``price_unavailable``、金额为空；
- 文字列以 ``= + - @`` 或制表、回车开头时前加单引号，表格软件不会当公式执行；
- 编码 UTF-8 带 BOM，便于中文表格软件直接打开。
"""
from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.tenancy import Principal

    from .service import OutcomeService

MINE_COLUMNS = ('task_id', 'capability_id', 'capability_revision', 'release_id', 'status', 'accepted_at',
                'finished_at', 'tokens_settled')
ADMIN_COLUMNS = ('capability_id', 'capability_revision', 'tasks', 'succeeded', 'failed', 'outcome_unknown',
                 'cancelled', 'interrupted', 'in_progress', 'unreadable', 'missing', 'technical_completion_rate',
                 'tokens_settled', 'tokens_held_upper_bound', 'tasks_without_token_records', 'cost_status',
                 'cost_amount')
_FORMULA = ('=', '+', '-', '@', '\t', '\r')


def safe(value: Any) -> str:
    """单元格文字：None 为空；以公式前缀开头的文字前加单引号。"""
    if value is None:
        return ''
    text = str(value)
    return "'" + text if isinstance(value, str) and text.startswith(_FORMULA) else text


def _csv(columns: tuple[str, ...], rows: list[dict[str, Any]]) -> bytes:
    """写成带 BOM 的 UTF-8 CSV。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\r\n')
    writer.writerow(columns)
    for row in rows:
        writer.writerow([safe(row.get(c)) for c in columns])
    return buffer.getvalue().encode('utf-8-sig')


async def mine_csv(service: OutcomeService, who: Principal, *, since: float | None,
                   until: float | None) -> bytes:
    """本人能力任务逐条导出（逐页取完，总数与页面一致）。"""
    rows: list[dict[str, Any]] = []
    offset: int | None = 0
    while offset is not None:
        page = await service.mine(who, since=since, until=until, limit=100, offset=offset)
        items = page['items']
        assert isinstance(items, list)
        rows.extend(dict(item) for item in items if isinstance(item, dict))
        nxt = page['next_offset']
        offset = nxt if isinstance(nxt, int) else None
    return _csv(MINE_COLUMNS, rows)


def _summary_row(item: dict[str, JsonValue]) -> dict[str, Any]:
    """一行能力修订的汇总。"""
    totals = item['totals']
    tokens = item['tokens']
    cost = item['cost']
    completion = item['technical_completion']
    assert isinstance(totals, dict) and isinstance(tokens, dict) and isinstance(cost, dict)
    assert isinstance(completion, dict)

    def count(key: str) -> int:
        value = totals.get(key, 0)
        return value if isinstance(value, int) else 0

    return {'capability_id': item['capability_id'], 'capability_revision': item['capability_revision'],
            **{k: count(k) for k in ('tasks', 'succeeded', 'failed', 'outcome_unknown', 'cancelled',
                                     'interrupted', 'unreadable', 'missing')},
            'in_progress': count('accepted') + count('running') + count('waiting_user'),
            'technical_completion_rate': completion.get('rate'),
            'tokens_settled': tokens.get('settled'), 'tokens_held_upper_bound': tokens.get('held_upper_bound'),
            'tasks_without_token_records': tokens.get('tasks_without_records'),
            'cost_status': cost.get('status'), 'cost_amount': cost.get('amount')}


async def admin_csv(service: OutcomeService, who: Principal, *, since: float | None,
                    until: float | None) -> bytes:
    """按能力与修订的汇总导出（仅管理员；不含任务或员工）。"""
    body = await service.admin(who, since=since, until=until)
    capabilities = body['capabilities']
    assert isinstance(capabilities, list)
    return _csv(ADMIN_COLUMNS, [_summary_row(c) for c in capabilities if isinstance(c, dict)])
