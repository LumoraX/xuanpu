"""显式维护入口，不自动停止服务、不覆盖旧数据、不启动业务worker。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from xuanpu_finance.maintenance import MaintenanceError, MaintenanceOptions, backup, restore


def main() -> None:
    """备份与恢复使用独立路径参数，凭据只从文件读取。"""
    parser = argparse.ArgumentParser(description='独立财务数据库与加密原件配套维护')
    parser.add_argument('action', choices=['backup', 'restore'])
    parser.add_argument('--database-dsn-file', required=True, type=Path)
    parser.add_argument('--objects', required=True, type=Path, help='独立local密文根；恢复时必须不存在')
    parser.add_argument('--key-file', required=True, type=Path, help='另行保全的32字节密钥，密钥不会写入备份')
    parser.add_argument('--package', required=True, type=Path, help='备份目标或恢复来源目录')
    parser.add_argument('--pg-tool', help='明确指定成熟pg_dump或pg_restore程序路径')
    args = parser.parse_args()
    options = MaintenanceOptions(args.database_dsn_file, args.objects, args.key_file)
    try:
        if args.action == 'backup':
            result = asyncio.run(backup(options, args.package, **({'pg_dump': args.pg_tool} if args.pg_tool else {})))
        else:
            result = asyncio.run(restore(options, args.package, **({'pg_restore': args.pg_tool} if args.pg_tool else {})))
    except (MaintenanceError, OSError, ValueError) as error:
        code = str(error) if isinstance(error, MaintenanceError) else 'MAINTENANCE_INPUT_INVALID'
        print(json.dumps({'status': 'failed', 'code': code}, ensure_ascii=False))
        raise SystemExit(1) from None
    print(json.dumps({'status': 'completed', **result}, ensure_ascii=False))


if __name__ == '__main__':
    main()
