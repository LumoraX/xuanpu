"""本人知识、受保护问答与能力结果命令（R4-D/F）：经公开 SDK 调用，凭据只从仅本人可读的文件读取。

命令不替员工自动选依据、确认记忆或登记核实结论；提问须给出操作号，断连重试复用同一操作号。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xuanpu_sdk import XuanpuClient, XuanpuError, XuanpuTransportError

from xuanpu_cli.work_commands import read_object

if TYPE_CHECKING:
    import argparse

OPERATIONS = ('sources', 'register', 'search', 'contexts', 'context', 'pin', 'ask', 'memories',
              'outcomes', 'admin-outcomes', 'batch')


def add_knowledge_commands(commands: Any) -> None:
    """``knowledge`` 子命令组。"""
    parent = commands.add_parser('knowledge', help='本人知识来源、依据材料提问、工作记忆与能力结果')
    parent.add_argument('--credentials', required=True, help='仅当前用户可读的私有凭据JSON')
    operations = parent.add_subparsers(dest='operation', required=True)
    for name in OPERATIONS:
        parser = operations.add_parser(name)
        if name in ('context', 'ask', 'batch'):
            parser.add_argument('identifier')
        if name == 'register':
            parser.add_argument('item_id')
            parser.add_argument('revision', type=int)
        if name == 'search':
            parser.add_argument('query')
            parser.add_argument('--limit', type=int, default=10)
        if name == 'pin':
            parser.add_argument('--request-id', required=True)
            parser.add_argument('--source', action='append', required=True,
                                help='依据：来源ID:块序号，可重复（来自检索结果）')
        if name == 'ask':
            parser.add_argument('--request-id', required=True)
            parser.add_argument('--question', required=True)
        if name in ('outcomes', 'admin-outcomes'):
            parser.add_argument('--since', type=float)
            parser.add_argument('--until', type=float)
        parser.set_defaults(handler=cmd_knowledge)


async def _credentials(path: str) -> dict[str, Any]:
    """凭据文件须仅本人可读，字段只允许地址、密钥与代理员工。"""
    if Path(path).stat().st_mode & 0o077:
        raise ValueError('凭据文件必须仅当前用户可读')
    credentials = await asyncio.to_thread(read_object, path)
    if set(credentials) - {'base_url', 'api_key', 'act_as_user'}:
        raise ValueError('凭据字段无效')
    return credentials


def _pins(values: list[str]) -> list[dict[str, Any]]:
    """把「来源ID:块序号」解析成依据列表。"""
    pins = []
    for value in values:
        source_id, _, ordinal = value.rpartition(':')
        if not source_id or not ordinal.isdigit():
            raise ValueError('依据格式应为 来源ID:块序号')
        pins.append({'source_id': source_id, 'ordinal': int(ordinal)})
    return pins


async def run_knowledge(args: argparse.Namespace) -> dict[str, Any]:
    """按操作调用一个 SDK 方法。"""
    credentials = await _credentials(args.credentials)
    async with XuanpuClient(**credentials) as client:
        op = args.operation
        if op == 'sources':
            return await client.list_knowledge_sources()
        if op == 'register':
            return await client.register_knowledge_source(args.item_id, args.revision)
        if op == 'search':
            return await client.search_knowledge(args.query, limit=args.limit)
        if op == 'contexts':
            return await client.list_knowledge_contexts()
        if op == 'context':
            return await client.read_knowledge_context(args.identifier)
        if op == 'pin':
            return await client.create_knowledge_context(args.request_id, _pins(args.source))
        if op == 'ask':
            return await client.ask_knowledge(args.identifier, args.request_id, args.question)
        if op == 'memories':
            return await client.list_work_memory()
        if op == 'outcomes':
            return await client.my_capability_outcomes(since=args.since, until=args.until)
        if op == 'admin-outcomes':
            return await client.admin_capability_outcomes(since=args.since, until=args.until)
        if op == 'batch':
            return await client.get_acceptance_batch(args.identifier)
    raise ValueError('未知知识操作')


def cmd_knowledge(args: argparse.Namespace) -> int:
    """错误不回显凭据或正文；成功打印服务端视图。"""
    try:
        result = asyncio.run(run_knowledge(args))
    except (OSError, ValueError, TypeError, KeyError, XuanpuError, XuanpuTransportError) as exc:
        code = getattr(exc, 'code', None)
        hint = '（依据来源已失效，请重新检索并选择依据）' if code == 'knowledge_source_revoked' else ''
        print(f'操作未完成，请检查私有凭据与权限；重试保留原操作号。{hint}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
