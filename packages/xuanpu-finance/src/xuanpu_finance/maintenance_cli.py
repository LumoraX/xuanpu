"""显式维护入口，不自动停止服务、不覆盖旧数据、不启动业务worker。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from xuanpu_finance.maintenance import MaintenanceError, MaintenanceOptions, backup, restore
from xuanpu_finance.maintenance_verify import verify_internal_records


def main() -> None:
    """按动作约束维护参数，凭据只从显式文件读取。"""
    parser = argparse.ArgumentParser(description='独立财务数据库与加密原件配套维护')
    parser.add_argument('action', choices=['backup', 'restore', 'verify'])
    parser.add_argument('--database-dsn-file', required=True, type=Path)
    parser.add_argument('--objects', required=True, type=Path, help='独立local密文根；恢复时必须不存在')
    parser.add_argument('--key-file', required=True, type=Path, help='另行保全的32字节密钥，密钥不会写入备份')
    parser.add_argument('--package', type=Path, help='备份目标或恢复来源目录')
    parser.add_argument('--pg-tool', help='明确指定成熟pg_dump或pg_restore程序路径')
    args = parser.parse_args()
    if args.action == 'verify':
        if args.package is not None or args.pg_tool is not None:
            parser.error('verify不接受--package或--pg-tool')
    elif args.package is None:
        parser.error('backup/restore必须提供--package')
    options = MaintenanceOptions(args.database_dsn_file, args.objects, args.key_file)
    try:
        if args.action == 'backup':
            assert args.package is not None
            result = asyncio.run(backup(options, args.package, **({'pg_dump': args.pg_tool} if args.pg_tool else {})))
        elif args.action == 'restore':
            assert args.package is not None
            result = asyncio.run(restore(options, args.package, **({'pg_restore': args.pg_tool} if args.pg_tool else {})))
        else:
            result = asyncio.run(verify_internal_records(options))
    except (MaintenanceError, OSError, ValueError) as error:
        code = str(error) if isinstance(error, MaintenanceError) else 'MAINTENANCE_INPUT_INVALID'
        print(json.dumps({'status': 'failed', 'code': code}, ensure_ascii=False))
        raise SystemExit(1) from None
    except Exception:
        # 未知内部故障也只能失败；旧backup/restore仍保留原有异常语义。
        if args.action != 'verify':
            raise
        print(json.dumps({'status': 'failed', 'code': 'VERIFICATION_FAILED'}, ensure_ascii=False))
        raise SystemExit(1) from None
    print(json.dumps({'status': 'completed', **result}, ensure_ascii=False))
    if args.action == 'verify' and result['verification_status'] == 'incomplete':
        raise SystemExit(2)


if __name__ == '__main__':
    main()
